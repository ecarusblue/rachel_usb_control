using UsbGuard;

var builder = Host.CreateApplicationBuilder(args);
builder.Services.AddWindowsService(o => o.ServiceName = Const.ServiceName);
builder.Services.AddHostedService<Worker>();
builder.Build().Run();
