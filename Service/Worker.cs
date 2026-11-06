using System.IO.Pipes;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text.Json;

namespace UsbGuard;

public sealed class Worker : BackgroundService
{
    readonly ILogger<Worker> _log;
    readonly object _lock = new();
    Config _cfg = ConfigStore.Load();
    DateTime? _tempUntil;
    int _fails;
    DateTime _lockedUntil = DateTime.MinValue;

    public Worker(ILogger<Worker> log) => _log = log;

    UsbMode Effective => _tempUntil > DateTime.UtcNow ? UsbMode.Allow : _cfg.Mode;

    protected override Task ExecuteAsync(CancellationToken ct) =>
        Task.WhenAll(EnforceLoop(ct), PipeLoop(ct));

    // 5초마다 정책을 재적용 -> 레지스트리를 수동으로 바꿔도 원복
    async Task EnforceLoop(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                lock (_lock)
                {
                    if (_tempUntil is not null && _tempUntil <= DateTime.UtcNow) _tempUntil = null;
                    Enforcer.Apply(Effective);
                }
            }
            catch (Exception e) { _log.LogError(e, "enforce failed"); }
            await Task.Delay(TimeSpan.FromSeconds(5), ct).ConfigureAwait(false);
        }
    }

    async Task PipeLoop(CancellationToken ct)
    {
        // 일반 사용자도 파이프에 접속은 가능(인증은 비밀번호로)
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

            switch (r.Cmd)
            {
                case "status":
                    return Ok("OK");
                case "setmode":
                    if (r.Mode is null) return Fail("mode 누락");
                    _cfg.Mode = r.Mode.Value; _tempUntil = null;
                    ConfigStore.Save(_cfg); Enforcer.Apply(Effective);
                    _log.LogInformation("mode -> {Mode}", _cfg.Mode);
                    return Ok($"모드 변경: {_cfg.Mode}");
                case "tempallow":
                    if (r.Minutes is < 1 or > 480) return Fail("1~480분 범위로 지정하세요.");
                    _tempUntil = DateTime.UtcNow.AddMinutes(r.Minutes);
                    Enforcer.Apply(Effective);
                    _log.LogInformation("temp allow {Min}min", r.Minutes);
                    return Ok($"{r.Minutes}분간 임시 허용");
                case "passwd":
                    if (string.IsNullOrWhiteSpace(r.NewPassword) || r.NewPassword.Length < 8)
                        return Fail("새 비밀번호는 8자 이상이어야 합니다.");
                    ConfigStore.SetPassword(_cfg, r.NewPassword); ConfigStore.Save(_cfg);
                    return Ok("비밀번호 변경 완료");
                default:
                    return Fail("알 수 없는 명령");
            }
        }
    }

    Response Ok(string m) => new()
    { Ok = true, Message = m, Mode = _cfg.Mode, Effective = Effective, TempUntil = _tempUntil };
    static Response Fail(string m) => new() { Ok = false, Message = m };
}
