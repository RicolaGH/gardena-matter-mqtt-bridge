package main

import (
 "bufio"
 "context"
 "encoding/base64"
 "encoding/binary"
 "encoding/json"
 "io"
 "net"
 "os"
 "path/filepath"
 "testing"
 "time"
)

func testConfig()Config{return Config{Host:"127.0.0.1",Port:1883,Prefix:"gardena",HAPrefix:"homeassistant",MowerIDs:[]string{"test-device"},Plan:[]Record{{Topic:"homeassistant/sensor/gardena_abcd/battery/config",Serial:"test-device",Resource:"battery_level",Key:"abcd",Config:map[string]any{"unique_id":"gardena_abcd_battery","state_topic":"gardena/abcd/battery_level/state","device":map[string]any{"identifiers":[]string{"gardena_abcd"}}}}}}}
func fixture(t *testing.T)string{t.Helper();root:=t.TempDir();base:=filepath.Join(root,"Device_descriptionID_1");files:=map[string]any{
 "Device_descriptionID_1.json":map[string]any{"serialid":"test-device"},
 "Value_description/Value_description_12.json":map[string]any{"name":"battery_level"},
 "Value/Value_12r.json":map[string]any{"value":"75"},
 };for p,v:=range files{p=filepath.Join(base,p);if e:=os.MkdirAll(filepath.Dir(p),0700);e!=nil{t.Fatal(e)};b,_:=json.Marshal(v);if e:=os.WriteFile(p,b,0600);e!=nil{t.Fatal(e)}};return root}
func TestSensorsPreservePlanAndFailOnMissing(t *testing.T){root:=fixture(t);cfg:=testConfig();v,e:=sensorValues(root,cfg.Plan);if e!=nil||len(v)!=1||v[0]!="75"{t.Fatalf("%v %v",v,e)};cfg.Plan[0].Resource="missing";if _,e=sensorValues(root,cfg.Plan);e==nil{t.Fatal("missing sensor accepted")}}
func TestCommandParity(t *testing.T){for _,gen:=range []string{"gen1","gen1_lona","gen2"}{m:=Mower{ID:"test",Generation:gen};for _,action:=range []string{"start_mowing","start_1h","start_3h","start_6h","dock"}{c,e:=m.command(action);if e!=nil{t.Fatal(e)};if c["request_id"]==""{t.Fatal("missing request ID")}}
 if _,e:=m.command("rm -rf /");e==nil{t.Fatal("unknown command")}}
 m:=Mower{ID:"test",Generation:"gen1_lona"};c,_:=m.command("start_3h");p:=c["payload"].(map[string]any);b,_:=base64.StdEncoding.DecodeString(p["vo"].(string));if binary.BigEndian.Uint32(b[2:])!=10800{t.Fatal("wrong duration")}
 m.Generation="gen1";c,_=m.command("dock");p=c["payload"].(map[string]any);b,_=base64.StdEncoding.DecodeString(p["vo"].(string));if binary.LittleEndian.Uint16(b)!=2042{t.Fatal("wrong park command")}
}
func TestMQTTSizeLimitAndRetain(t *testing.T){a,b:=net.Pipe();defer a.Close();defer b.Close();m:=&mqttClient{Conn:a,reader:bufio.NewReader(a)};go func(){b.Write([]byte{0x30,0xff,0xff,0xff,0x7f})}();if _,e:=m.receive();e==nil{t.Fatal("oversize accepted")};topic,action,retained,e:=parsePublish(mqttPacket{0x31,append(mqttString("gardena/abcd/mower/command"),[]byte("dock")...)});if e!=nil||!retained||topic==""||action!="dock"{t.Fatal("publish parsing")}}
func TestWebSocketFrameLimit(t *testing.T){a,b:=net.Pipe();defer a.Close();defer b.Close();w:=&websocket{Conn:a};go func(){b.Write([]byte{0x81,0x7f,0,0,1,0,0,0,0,0})}();if _,e:=w.receive();e==nil{t.Fatal("oversize accepted")}}
func TestConfigPermissions(t *testing.T){path:=filepath.Join(t.TempDir(),"config.json");b,_:=json.Marshal(testConfig());os.WriteFile(path,b,0644);if _,e:=loadConfig(path);e==nil{t.Fatal("public config accepted")};os.Chmod(path,0600);if _,e:=loadConfig(path);e!=nil{t.Fatal(e)}}

func readClientFrame(conn net.Conn)(byte,[]byte,error){var h [2]byte;if _,e:=io.ReadFull(conn,h[:]);e!=nil{return 0,nil,e};n:=int(h[1]&127);if n==126{var b [2]byte;if _,e:=io.ReadFull(conn,b[:]);e!=nil{return 0,nil,e};n=int(binary.BigEndian.Uint16(b[:]))};if n>65535{return 0,nil,errProtocol};var mask [4]byte;if _,e:=io.ReadFull(conn,mask[:]);e!=nil{return 0,nil,e};b:=make([]byte,n);if _,e:=io.ReadFull(conn,b);e!=nil{return 0,nil,e};for i:=range b{b[i]^=mask[i%4]};return h[0]&15,b,nil}
func sendServerFrame(conn net.Conn,payload any)error{b,_:=json.Marshal(payload);h:=[]byte{0x81};if len(b)<126{h=append(h,byte(len(b)))}else{h=append(h,126,byte(len(b)>>8),byte(len(b)))};_,e:=conn.Write(append(h,b...));return e}

func TestGatewaySessionWorksWithoutHAAndRejectsRetainedCommand(t *testing.T){
 // Only a broker, local gateway API and sensor files exist in this integration test.
 // No installer, HA process, SSH connection or HA API is started.
 root:=fixture(t);cfg:=testConfig();listener,e:=net.Listen("tcp","127.0.0.1:0");if e!=nil{t.Fatal(e)};defer listener.Close();cfg.Port=listener.Addr().(*net.TCPAddr).Port
 a,b:=net.Pipe();defer b.Close();oldAPI,oldWS:=prepareLocalAPI,openLocalWS;defer func(){prepareLocalAPI=oldAPI;openLocalWS=oldWS}();prepareLocalAPI=func(string)error{return nil};openLocalWS=func(string)(*websocket,error){return &websocket{Conn:a},nil}
 commands:=make(chan string,4)
 go func(){defer b.Close();for{_,body,e:=readClientFrame(b);if e!=nil{return};var requests []Message;if json.Unmarshal(body,&requests)!=nil{return};var responses []map[string]any;for _,request:=range requests{
  payload:=map[string]any{};if request.Op=="read"{if request.Entity["service"]=="lemonbeatd"{payload["test-device"]=map[string]any{"device":map[string]any{"0":map[string]any{"model_number":map[string]any{"vs":"6146"}}},"lemonbeat":map[string]any{"0":map[string]any{"status":map[string]any{"vi":8}}}}}}else{commands<-request.Op}
  responses=append(responses,map[string]any{"request_id":request.RequestID,"success":true,"payload":payload})};if sendServerFrame(b,responses)!=nil{return}}}()
 ctx,cancel:=context.WithCancel(context.Background());defer cancel();finished:=make(chan error,1);go func(){finished<-session(ctx,cfg,root)}()
 conn,e:=listener.Accept();if e!=nil{t.Fatal(e)};defer conn.Close();broker:=&mqttClient{Conn:conn,reader:bufio.NewReader(conn)}
 p,e:=broker.receive();if e!=nil||p.header!=0x10{t.Fatal("CONNECT missing")};broker.send(0x20,[]byte{0,0});p,e=broker.receive();if e!=nil||p.header!=0x82{t.Fatal("SUBSCRIBE missing")};broker.send(0x90,[]byte{0,1,0})
 for{p,e=broker.receive();if e!=nil{t.Fatal(e)};topic,body,_,_:=parsePublish(p);if topic==cfg.availability()&&body=="online"{break}}
 // Stop/no HA installer has no effect: a fresh live command still reaches the gateway API.
 broker.publish("gardena/abcd/mower/command","dock",true)
 broker.publish("gardena/abcd/mower/command","dock",false)
 select{case op:=<-commands:if op!="write"{t.Fatal("wrong operation")};case <-time.After(3*time.Second):t.Fatal("command not delivered")}
 select{case <-commands:t.Fatal("retained command executed");case <-time.After(50*time.Millisecond):}
 // Broker loss is detected; the main service loop can reconnect independently.
 conn.Close();select{case e:=<-finished:if e==nil{t.Fatal("broker loss not reported")};case <-time.After(3*time.Second):t.Fatal("runtime did not detect disconnect")}
}
