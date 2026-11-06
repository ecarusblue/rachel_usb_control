namespace UsbGuard;

public static class Const
{
    public const string PipeName = "UsbGuardPipe";
    public const string ServiceName = "UsbGuardSvc";
}

public enum UsbMode { Block, ReadOnly, Allow }

// 한 줄 JSON 요청/응답
public sealed class Request
{
    public string Cmd { get; set; } = "";        // status | setmode | tempallow | passwd | setup
    public string Password { get; set; } = "";
    public string? NewPassword { get; set; }
    public UsbMode? Mode { get; set; }
    public int Minutes { get; set; }
}

public sealed class Response
{
    public bool Ok { get; set; }
    public string Message { get; set; } = "";
    public UsbMode? Mode { get; set; }
    public UsbMode? Effective { get; set; }
    public DateTime? TempUntil { get; set; }
}
