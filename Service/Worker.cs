using System.IO.Pipes;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text.Json;

namespace UsbGuard;

/// <summary>정책 적용(5초 주기 재적용) + 로컬 CLI(Named Pipe) 처리. 서버 연동 시 Reporter 가 정책을 갱신한다.</summary>
public sealed class Worker : BackgroundService
{
    public const string Version = "0.2.0";

    readonly ILogger<Worker> _log;
    readonly object _lock = new();
    readonly Config _cfg = ConfigStore.Load();

    DateTime? _tempUntil;
    int _fails;
    DateTime _lockedUntil = DateTime.MinValue;

    // 변조 감지: 같은 모드를 적용 중인데 레지스트리가 달라져 있었다면 누군가 바꾼 것
    UsbMode? _lastApplied;
    int _tamper;
    DateTime? _lastTamper, _lastEnforce;

    public Worker(ILogger<Worker> log) => _log = log;

    UsbMode Effective => _tempUntil > DateTime.UtcNow ? UsbMode.Allow : _cfg.Mode;

    bool IsManaged => !string.IsNullOrEmpty(_cfg.ServerUrl) && !string.IsNullOrEmpty(_cfg.AgentToken);

    // ───────── Reporter 용 API ─────────
    public (string Url, string Token)? ServerInfo()
    {
        lock (_lock) return IsManaged ? (_cfg.ServerUrl!, _cfg.AgentToken!) : null;
    }

    public Heartbeat Snapshot()
    {
        var devices = UsbScanner.Scan();   // WMI 는 느릴 수 있어 락 밖에서
        lock (_lock)
        {
            var (start, wp) = Enforcer.Read();
            return new Heartbeat
            {
                Hostname = Environment.MachineName,
                Version = Version,
                ConfiguredMode = _cfg.Mode,
                EffectiveMode = Effective,
                UsbstorStart = start,
                WriteProtect = wp,
                UsbDevices = devices,
                TamperCount = _tamper,
                LastEnforceUtc = _lastEnforce,
                LastTamperUtc = _lastTamper,
                TempUntilUtc = _tempUntil > DateTime.UtcNow ? _tempUntil : null,
            };
        }
    }

    /// <summary>서버가 내려준 정책 반영. 모드는 설정에 저장해 두므로 서버가 끊겨도 마지막 정책을 유지한다.</summary>
    public void ApplyRemote(UsbMode mode, DateTime? tempUntilUtc)
    {
        lock (_lock)
        {
            if (!IsManaged) return;
            if (_cfg.Mode != mode)
            {
                _cfg.Mode = mode;
                ConfigStore.Save(_cfg);
                _log.LogInformation("server policy -> {Mode}", mode);
            }
            _tempUntil = tempUntilUtc > DateTime.UtcNow ? tempUntilUtc : null;
            EnforceOnce();
        }
    }

    // ───────── 정책 적용 ─────────
    void EnforceOnce()   // 호출 시 _lock 보유
    {
        if (_tempUntil is not null && _tempUntil <= DateTime.UtcNow) _tempUntil = null;
        var eff = Effective;
        bool changed = Enforcer.Apply(eff);
        if (changed && _lastApplied == eff)
        {
            _tamper++;
            _lastTamper = DateTime.UtcNow;
            _log.LogWarning("tamper detected: registry restored (mode={Mode}, count={N})", eff, _tamper);
        }
        _lastApplied = eff;
        _lastEnforce = DateTime.UtcNow;
    }

    protected override Task ExecuteAsync(CancellationToken ct) =>
        Task.WhenAll(EnforceLoop(ct), PipeLoop(ct));

    async Task EnforceLoop(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { lock (_lock) EnforceOnce(); }
            catch (Exception e) { _log.LogError(e, "enforce failed"); }
            await Task.Delay(TimeSpan.FromSeconds(5), ct).ConfigureAwait(false);
        }
    }

    // ───────── 로컬 CLI(Named Pipe) ─────────
    async Task PipeLoop(CancellationToken ct)
    {
        // 일반 사용자도 파이프 접속은 가능(변경 명령은 비밀번호로 인증)
        var sec = new PipeSecurity();
        sec.AddAccessRule(new PipeAccessRule(
            new SecurityIdentifier(WellKnownSidType.AuthenticatedUserSid, null),
            PipeAccessRights.ReadWrite, AccessControlType.Allow));
        sec.AddAccessRule(new PipeAccessRule(
            new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null),
            PipeAccessRights.FullControl, AccessControlType.Allow));

        while (!ct.IsCancellationRequested)
        {
            using var server = NamedPipeServerStreamAcl.Create(
                Const.PipeName, PipeDirection.InOut, 1, PipeTransmissionMode.Byte,
                PipeOptions.Asynchronous, 4096, 4096, sec);
            try
            {
                await server.WaitForConnectionAsync(ct);
                using var reader = new StreamReader(server);
                using var writer = new StreamWriter(server) { AutoFlush = true };
                var line = await reader.ReadLineAsync(ct);
                var req = JsonSerializer.Deserialize<Request>(line ?? "{}") ?? new Request();
                var res = Handle(req);
                await writer.WriteLineAsync(JsonSerializer.Serialize(res));
            }
            catch (OperationCanceledException) { break; }
            catch (Exception e) { _log.LogWarning(e, "pipe error"); }
        }
    }

    Response Handle(Request r)
    {
        lock (_lock)
        {
            // 최초 설정: 비밀번호가 아직 없을 때만 허용
            if (r.Cmd == "setup")
            {
                if (_cfg.Hash is not null) return Fail("이미 설정되었습니다.");
                if (string.IsNullOrWhiteSpace(r.NewPassword) || r.NewPassword.Length < 8)
                    return Fail("비밀번호는 8자 이상이어야 합니다.");
                ConfigStore.SetPassword(_cfg, r.NewPassword);
                ConfigStore.Save(_cfg);
                _log.LogInformation("password initialized");
                return Ok("초기 비밀번호가 설정되었습니다.");
            }

            // 조회(status)는 인증 없이 허용, 나머지는 비밀번호 필요
            if (r.Cmd != "status")
            {
                if (DateTime.UtcNow < _lockedUntil)
                    return Fail($"잠금 상태입니다. {(_lockedUntil - DateTime.UtcNow).TotalSeconds:0}초 후 재시도하세요.");

                if (!ConfigStore.Verify(_cfg, r.Password))
                {
                    _fails++;
                    if (_fails >= 5) { _lockedUntil = DateTime.UtcNow.AddMinutes(5); _fails = 0; }
                    _log.LogWarning("auth failed (cmd={Cmd})", r.Cmd);
                    return Fail("인증 실패");
                }
                _fails = 0;
            }

            const string ManagedMsg = "서버 관리 중에는 로컬에서 모드를 바꿀 수 없습니다. 서버 GUI에서 변경하거나 'unenroll' 하세요.";

            switch (r.Cmd)
            {
                case "status":
                    return Ok(IsManaged ? "서버 관리 모드" : "로컬 관리 모드");
                case "setmode":
                    if (IsManaged) return Fail(ManagedMsg);
                    if (r.Mode is null) return Fail("mode 누락");
                    _cfg.Mode = r.Mode.Value; _tempUntil = null;
                    ConfigStore.Save(_cfg); EnforceOnce();
                    _log.LogInformation("mode -> {Mode}", _cfg.Mode);
                    return Ok($"모드 변경: {_cfg.Mode}");
                case "tempallow":
                    if (IsManaged) return Fail(ManagedMsg);
                    if (r.Minutes is < 1 or > 480) return Fail("1~480분 범위로 지정하세요.");
                    _tempUntil = DateTime.UtcNow.AddMinutes(r.Minutes);
                    EnforceOnce();
                    _log.LogInformation("temp allow {Min}min", r.Minutes);
                    return Ok($"{r.Minutes}분간 임시 허용");
                case "passwd":
                    if (string.IsNullOrWhiteSpace(r.NewPassword) || r.NewPassword.Length < 8)
                        return Fail("새 비밀번호는 8자 이상이어야 합니다.");
                    ConfigStore.SetPassword(_cfg, r.NewPassword); ConfigStore.Save(_cfg);
                    return Ok("비밀번호 변경 완료");
                case "enroll":
                    if (!Uri.TryCreate(r.ServerUrl, UriKind.Absolute, out var u) ||
                        (u.Scheme != Uri.UriSchemeHttp && u.Scheme != Uri.UriSchemeHttps))
                        return Fail("서버 주소는 http(s)://host:port 형식이어야 합니다.");
                    if (string.IsNullOrWhiteSpace(r.AgentToken)) return Fail("토큰 누락");
                    _cfg.ServerUrl = u.GetLeftPart(UriPartial.Authority);
                    _cfg.AgentToken = r.AgentToken.Trim();
                    ConfigStore.Save(_cfg);
                    _log.LogInformation("enrolled to {Url}", _cfg.ServerUrl);
                    return Ok($"서버 등록 완료: {_cfg.ServerUrl} (30초 이내 첫 보고)");
                case "unenroll":
                    _cfg.ServerUrl = null; _cfg.AgentToken = null;
                    ConfigStore.Save(_cfg);
                    return Ok("서버 연동 해제 (로컬 관리 모드)");
                default:
                    return Fail("알 수 없는 명령");
            }
        }
    }

    Response Ok(string m) => new()
    { Ok = true, Message = m, Mode = _cfg.Mode, Effective = Effective, TempUntil = _tempUntil, Managed = IsManaged };
    static Response Fail(string m) => new() { Ok = false, Message = m };
}
