using System.Net.Http.Headers;
using System.Net.Http.Json;

namespace UsbGuard;

/// <summary>30초마다 중앙 서버로 상태를 보고하고, 응답으로 받은 정책을 Worker 에 반영한다. (PC → 서버 방향 연결만 사용)</summary>
public sealed class Reporter : BackgroundService
{
    static readonly TimeSpan Interval = TimeSpan.FromSeconds(30);

    readonly Worker _worker;
    readonly ILogger<Reporter> _log;
    readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(10) };

    public Reporter(Worker worker, ILogger<Reporter> log)
    {
        _worker = worker;
        _log = log;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                var info = _worker.ServerInfo();
                if (info is not null) await SendOnce(info.Value.Url, info.Value.Token, ct);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception e)
            {
                // 서버 불가 시: 마지막으로 받은 정책을 그대로 유지
                _log.LogWarning("heartbeat failed: {Msg}", e.Message);
            }
            await Task.Delay(Interval, ct).ConfigureAwait(false);
        }
    }

    async Task SendOnce(string url, string token, CancellationToken ct)
    {
        var hb = _worker.Snapshot();
        using var req = new HttpRequestMessage(HttpMethod.Post, url.TrimEnd('/') + "/api/agent/heartbeat")
        {
            Content = JsonContent.Create(hb, options: WireJson.Options),
        };
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);

        using var res = await _http.SendAsync(req, ct);
        if (!res.IsSuccessStatusCode)
        {
            _log.LogWarning("heartbeat rejected: HTTP {Code}", (int)res.StatusCode);
            return;
        }
        var reply = await res.Content.ReadFromJsonAsync<HeartbeatReply>(WireJson.Options, ct);
        if (reply is not null) _worker.ApplyRemote(reply.Mode, reply.TempUntilUtc);
    }
}
