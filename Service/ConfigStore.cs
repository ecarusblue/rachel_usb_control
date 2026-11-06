using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text.Json;

namespace UsbGuard;

public sealed class Config
{
    public UsbMode Mode { get; set; } = UsbMode.Block;
    public string? Salt { get; set; }   // base64
    public string? Hash { get; set; }   // base64 (PBKDF2-SHA256)
    public int Iterations { get; set; } = 200_000;

    // 중앙 서버 연동 (둘 다 있으면 "서버 관리" 모드)
    public string? ServerUrl { get; set; }
    public string? AgentToken { get; set; }
}

public static class ConfigStore
{
    static readonly string Dir =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "UsbGuard");
    static readonly string FilePath = Path.Combine(Dir, "config.json");

    public static Config Load()
    {
        EnsureDir();
        if (!File.Exists(FilePath)) return new Config();
        return JsonSerializer.Deserialize<Config>(File.ReadAllText(FilePath)) ?? new Config();
    }

    public static void Save(Config c)
    {
        EnsureDir();
        File.WriteAllText(FilePath, JsonSerializer.Serialize(c));
    }

    // SYSTEM / Administrators 만 접근 가능하도록 ACL 고정
    static void EnsureDir()
    {
        if (Directory.Exists(Dir)) return;
        var di = Directory.CreateDirectory(Dir);
        var sec = new DirectorySecurity();
        sec.SetAccessRuleProtection(true, false); // 상속 제거
        var inherit = InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit;
        foreach (var sid in new[] { WellKnownSidType.LocalSystemSid, WellKnownSidType.BuiltinAdministratorsSid })
            sec.AddAccessRule(new FileSystemAccessRule(
                new SecurityIdentifier(sid, null), FileSystemRights.FullControl,
                inherit, PropagationFlags.None, AccessControlType.Allow));
        di.SetAccessControl(sec);
    }

    public static void SetPassword(Config c, string password)
    {
        var salt = RandomNumberGenerator.GetBytes(16);
        c.Salt = Convert.ToBase64String(salt);
        c.Hash = Convert.ToBase64String(Derive(password, salt, c.Iterations));
    }

    public static bool Verify(Config c, string password)
    {
        if (c.Salt is null || c.Hash is null) return false;
        var actual = Derive(password, Convert.FromBase64String(c.Salt), c.Iterations);
        return CryptographicOperations.FixedTimeEquals(actual, Convert.FromBase64String(c.Hash));
    }

    static byte[] Derive(string pw, byte[] salt, int iter) =>
        Rfc2898DeriveBytes.Pbkdf2(pw, salt, iter, HashAlgorithmName.SHA256, 32);
}
