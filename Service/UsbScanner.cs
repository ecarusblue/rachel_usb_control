using System.Management;

namespace UsbGuard;

// 현재 연결된 USB 저장장치 목록 (차단이 실제로 먹혔는지 확인용)
// 참고: UASP 방식 외장 SSD 등은 InterfaceType 이 'SCSI' 로 보일 수 있어 누락될 수 있음.
public static class UsbScanner
{
    public static List<UsbDevice> Scan()
    {
        var list = new List<UsbDevice>();
        try
        {
            using var s = new ManagementObjectSearcher(
                "SELECT Model, SerialNumber, PNPDeviceID FROM Win32_DiskDrive WHERE InterfaceType='USB'");
            foreach (ManagementBaseObject o in s.Get())
            {
                list.Add(new UsbDevice
                {
                    Model = o["Model"] as string ?? "",
                    Serial = (o["SerialNumber"] as string)?.Trim(),
                    PnpId = o["PNPDeviceID"] as string ?? "",
                });
                o.Dispose();
            }
        }
        catch
        {
            // WMI 실패 시 빈 목록 (보고는 계속)
        }
        return list;
    }
}
