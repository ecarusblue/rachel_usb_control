using UsbGuard;

var builder = Host.CreateApplicationBuilder(args);
builder.Services.AddWindowsService(o => o.ServiceName = Const.ServiceName);

// Worker 를 Reporter 와 공유하기 위해 싱글톤으로 등록
builder.Services.AddSingleton<Worker>();
builder.Services.AddHostedService(sp => sp.GetRequiredService<Worker>());
builder.Services.AddHostedService<Reporter>();

builder.Build().Run();
