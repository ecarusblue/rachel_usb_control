using System.Text.Json;
using System.Text.Json.Serialization;

namespace UsbGuard;

public static class Const
{
    public const string PipeName = "UsbGuardPipe";
    public const string ServiceName = "UsbGuardSvc";
}

public enum UsbMode { Block, ReadOnly, Allow }

// ───────── 로컬 Named Pipe (CLI <-> 서비스) ─────────
public sealed class Request
{
    public string Cmd { get; set; } = "";   // status | setmode | tempallow | passwd | setup | enroll | unenroll
    public string Password { get; set; } = "";
    public string? NewPassword { get; set; }
    public UsbMode? Mode { get; set; }
    public int Minutes { get; set; }
    public string? ServerUrl { get; set; }
    public string? AgentToken { get; set; }
}

public sealed class Response
{
    public bool Ok { get; set; }
    public string Message { get; set; } = "";
    public UsbMode? Mode { get; set; }
    public UsbMode? Effective { get; set; }
    public DateTime? TempUntil { get; set; }
    public bool Managed { get; set; }
}

// ───────── 서버 통신 (에이전트 <-> 중앙 서버), snake_case JSON ─────────
public static class WireJson
{
    public static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        Converters = { new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower) },
    };
}

public sealed class UsbDevice
{
    public string Model { get; set; } = "";
    public string? Serial { get; set; }
    public string PnpId { get; set; } = "";
}

public sealed class Heartbeat
{
    public string Hostname { get; set; } = "";
    public string Version { get; set; } = "";
    public UsbMode ConfiguredMode { get; set; }
    public UsbMode EffectiveMode { get; set; }
    public int? UsbstorStart { get; set; }      // 레지스트리 실제값 (4=차단)
    public int? WriteProtect { get; set; }      // 레지스트리 실제값 (1=읽기전용)
    public List<UsbDevice> UsbDevices { get; set; } = new();
    public int TamperCount { get; set; }
    public DateTime? LastEnforceUtc { get; set; }
    public DateTime? LastTamperUtc { get; set; }
    public DateTime? TempUntilUtc { get; set; }
}

public sealed class HeartbeatReply
{
    public UsbMode Mode { get; set; }
    public DateTime? TempUntilUtc { get; set; }
}
