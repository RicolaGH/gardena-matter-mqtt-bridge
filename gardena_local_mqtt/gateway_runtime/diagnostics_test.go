package main

import (
    "bufio"
    "bytes"
    "context"
    "encoding/json"
    "errors"
    "net"
    "os"
    "path/filepath"
    "testing"
    "time"
)

func TestDiagnosticsAreBoundedAndNeverIncludeRawError(t *testing.T){
    d:=newDiagnostics();d.stage("mqtt_connect")
    for i:=0;i<100;i++{d.failure(atStage("mqtt_read",errors.New("SECRET_PASSWORD_AND_DEVICE")))}
    s:=d.snapshot();if len(s.Events)!=32||s.Events[0].Stage!="mqtt_read"{t.Fatal("history not bounded/classified")}
    b,_:=json.Marshal(s);if bytes.Contains(b,[]byte("SECRET")){t.Fatal("raw error leaked")}
    if errorKind(&net.DNSError{IsTimeout:true})!="timeout"{t.Fatal("timeout not classified")}
    path:=filepath.Join(t.TempDir(),"diagnostics.json");if e:=d.write(path);e!=nil{t.Fatal(e)}
    info,e:=os.Stat(path);if e!=nil||info.Mode().Perm()!=0600{t.Fatal("diagnostics not private")}
}

func TestBlockedSensorReadIsVisibleAndExplainsHeartbeatGap(t *testing.T){
    // Reproduce the current scheduling coupling, without lengthening network timeouts.
    // Independent diagnostic snapshots must continue while the main loop is blocked.
    cfg:=testConfig();cfg.MowerIDs=nil;root:=fixture(t)
    listener,e:=net.Listen("tcp","127.0.0.1:0");if e!=nil{t.Fatal(e)};defer listener.Close()
    cfg.Port=listener.Addr().(*net.TCPAddr).Port
    oldAPI,oldWS,oldRead:=prepareLocalAPI,openLocalWS,readSensorValues
    oldPoll,oldHeartbeat,oldDiag:=pollInterval,heartbeatInterval,diagnostics
    defer func(){prepareLocalAPI=oldAPI;openLocalWS=oldWS;readSensorValues=oldRead;pollInterval=oldPoll;heartbeatInterval=oldHeartbeat;diagnostics=oldDiag}()
    diagnostics=newDiagnostics();d:=diagnostics;pollInterval=120*time.Millisecond;heartbeatInterval=20*time.Millisecond
    blocked,release:=make(chan struct{}),make(chan struct{});released:=false
    reads:=0;readSensorValues=func(root string,plan []Record)([]string,error){reads++;if reads==2{close(blocked);<-release};return sensorValues(root,plan)}
    prepareLocalAPI=func(string)error{return nil}
    openLocalWS=func(string)(*websocket,error){a,b:=net.Pipe();go func(){defer b.Close();for{
        op,body,e:=readClientFrame(b);if e!=nil{return};if op==9{if _,e=b.Write([]byte{0x8a,0});e!=nil{return};continue}
        var requests []Message;if json.Unmarshal(body,&requests)!=nil{return};var replies []map[string]any
        for _,r:=range requests{replies=append(replies,map[string]any{"request_id":r.RequestID,"success":true,"payload":map[string]any{}})}
        if sendServerFrame(b,replies)!=nil{return}
    }}();return &websocket{Conn:a},nil}
    ctx,cancel:=context.WithCancel(context.Background())
    finished:=make(chan error,1);go func(){finished<-session(ctx,cfg,root)}()
    defer func(){if !released{close(release)};cancel();select{case <-finished:case <-time.After(3*time.Second):t.Error("session did not stop")}}()
    listener.(*net.TCPListener).SetDeadline(time.Now().Add(3*time.Second))
    conn,e:=listener.Accept();if e!=nil{t.Fatal(e)};defer conn.Close()
    brokerDone:=make(chan struct{});go func(){defer close(brokerDone);broker:=&mqttClient{Conn:conn,reader:bufio.NewReader(conn)};for{
        p,e:=broker.receive();if e!=nil{return};switch p.header{case 0x10:broker.send(0x20,[]byte{0,0});case 0x82:broker.send(0x90,[]byte{0,1,0});case 0xc0:broker.send(0xd0,nil)}
    }}()
    defer func(){conn.Close();<-brokerDone}()
    select{case <-blocked:case <-time.After(3*time.Second):t.Fatal("sensor read not reached")}
    path:=filepath.Join(t.TempDir(),"diagnostics.json")
    recordCtx,stopRecord:=context.WithCancel(context.Background());recordDone:=make(chan struct{})
    go func(){d.record(recordCtx,path,10*time.Millisecond);close(recordDone)}()
    defer func(){stopRecord();<-recordDone}()
    before:=d.snapshot();time.Sleep(150*time.Millisecond)
    raw,e:=os.ReadFile(path);if e!=nil{t.Fatal(e)};var snap diagnosticSnapshot;if json.Unmarshal(raw,&snap)!=nil{t.Fatal("invalid snapshot")}
    if snap.Stage!="sensor_read"||snap.StageMS<80{t.Fatalf("blocked read not visible: %+v",snap)}
    if snap.Pings!=before.Pings{t.Fatal("expected to reproduce heartbeat gap during blocked read")}
    close(release);released=true
    deadline:=time.Now().Add(time.Second)
    for d.snapshot().Pings<=before.Pings&&time.Now().Before(deadline){time.Sleep(10*time.Millisecond)}
    if d.snapshot().Pings<=before.Pings{t.Fatal("heartbeats did not resume")}
}
