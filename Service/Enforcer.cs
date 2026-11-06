using Microsoft.Win32;

namespace UsbGuard;

// USB 저장장치 정책을 레지스트리에 적용
//  Block    : USBSTOR 드라이버 비활성(Start=4) -> 새로 꽂는 USB 메모리 인식 안 됨
//  ReadOnly : 드라이버 활성 + WriteProtect=1   -> 읽기만 가능
//  Allow    : 드라이버 활성 + WriteProtect=0
public static class Enforcer
{
    const string UsbStor = @"SYSTEM\CurrentControlSet\Services\USBSTOR";
    const string StoragePolicy = @"SYSTEM\CurrentControlSet\Control\StorageDevicePolicies";

    /// <summary>정책을 적용하고, 레지스트리 값을 실제로 바꿨으면 true.</summary>
    public static bool Apply(UsbMode mode)
    {
        int start = mode == UsbMode.Block ? 4 : 3;
        int writeProtect = mode == UsbMode.ReadOnly ? 1 : 0;
        bool changed = false;

        using (var k = Registry.LocalMachine.CreateSubKey(UsbStor, true))
        {
            if ((k.GetValue("Start") as int?) != start)
            {
                k.SetValue("Start", start, RegistryValueKind.DWord);
                changed = true;
            }
        }
        using (var k = Registry.LocalMachine.CreateSubKey(StoragePolicy, true))
        {
            if ((k.GetValue("WriteProtect") as int?) != writeProtect)
            {
                k.SetValue("WriteProtect", writeProtect, RegistryValueKind.DWord);
                changed = true;
            }
        }
        return changed;
    }

    /// <summary>현재 레지스트리 실제값(보고용). 값이 없으면 null.</summary>
    public static (int? Start, int? WriteProtect) Read()
    {
        int? start = null, wp = null;
        using (var k = Registry.LocalMachine.OpenSubKey(UsbStor))
            start = k?.GetValue("Start") as int?;
        using (var k = Registry.LocalMachine.OpenSubKey(StoragePolicy))
            wp = k?.GetValue("WriteProtect") as int?;
        return (start, wp);
    }
}
