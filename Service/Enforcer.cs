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

    public static void Apply(UsbMode mode)
    {
        int start = mode == UsbMode.Block ? 4 : 3;
        int writeProtect = mode == UsbMode.ReadOnly ? 1 : 0;

        using (var k = Registry.LocalMachine.CreateSubKey(UsbStor, true))
        {
            if ((k.GetValue("Start") as int?) != start)
                k.SetValue("Start", start, RegistryValueKind.DWord);
        }
        using (var k = Registry.LocalMachine.CreateSubKey(StoragePolicy, true))
        {
            if ((k.GetValue("WriteProtect") as int?) != writeProtect)
                k.SetValue("WriteProtect", writeProtect, RegistryValueKind.DWord);
        }
    }
}
