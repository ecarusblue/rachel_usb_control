using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using UsbGuard;

// 사용법
//   usbguard setup                 최초 비밀번호 설정
//   usbguard status                현재 상태
//   usbguard block|readonly|allow  모드 변경 (비밀번호 필요)
//   usbguard tempallow <분>        임시 허용 (비밀번호 필요)
//   usbguard passwd                비밀번호 변경

if (args.Length == 0) { Usage(); return 1; }

var req = new Request { Cmd = args[0].ToLowerInvariant() };
switch (req.Cmd)
{
    case "status": break;
    case "setup":
        req.NewPassword = ReadPassword("새 비밀번호(8자 이상): ");
        if (ReadPassword("확인: ") != req.NewPassword) { Console.WriteLine("불일치"); return 1; }
        break;
    case "block": req.Cmd = "setmode"; req.Mode = UsbMode.Block; req.Password = ReadPassword("관리자 비밀번호: "); break;
    case "readonly": req.Cmd = "setmode"; req.Mode = UsbMode.ReadOnly; req.Password = ReadPassword("관리자 비밀번호: "); break;
    case "allow": req.Cmd = "setmode"; req.Mode = UsbMode.Allow; req.Password = ReadPassword("관리자 비밀번호: "); break;
    case "tempallow":
        if (args.Length < 2 || !int.TryParse(args[1], out var min)) { Usage(); return 1; }
        req.Minutes = min; req.Password = ReadPassword("관리자 비밀번호: "); break;
    case "passwd":
        req.Password = ReadPassword("현재 비밀번호: ");
        req.NewPassword = ReadPassword("새 비밀번호: ");
        break;
    default: Usage(); return 1;
}

try
{
    using var pipe = new NamedPipeClientStream(".", Const.PipeName, PipeDirection.InOut);
    pipe.Connect(3000);
    using var w = new StreamWriter(pipe) { AutoFlush = true };
    using var r = new StreamReader(pipe);
    w.WriteLine(JsonSerializer.Serialize(req));
    var res = JsonSerializer.Deserialize<Response>(r.ReadLine() ?? "{}")!;
    Console.WriteLine(res.Message);
    if (res.Ok)
    {
        Console.WriteLine($"설정 모드: {res.Mode} / 현재 적용: {res.Effective}"
            + (res.TempUntil is null ? "" : $" (임시허용 만료: {res.TempUntil.Value.ToLocalTime():HH:mm:ss})"));
    }
    return res.Ok ? 0 : 2;
}
catch (Exception e)
{
    Console.WriteLine($"서비스에 연결할 수 없습니다: {e.Message}");
    return 3;
}

static void Usage() =>
    Console.WriteLine("usbguard setup | status | block | readonly | allow | tempallow <분> | passwd");

static string ReadPassword(string prompt)
{
    Console.Write(prompt);
    var sb = new StringBuilder();
    ConsoleKeyInfo k;
    while ((k = Console.ReadKey(true)).Key != ConsoleKey.Enter)
    {
        if (k.Key == ConsoleKey.Backspace) { if (sb.Length > 0) sb.Length--; }
        else if (!char.IsControl(k.KeyChar)) sb.Append(k.KeyChar);
    }
    Console.WriteLine();
    return sb.ToString();
}
