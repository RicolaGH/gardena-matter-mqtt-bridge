// GARDENA Local MQTT gateway runtime. Built from this repository, stdlib only.
package main

import (
 "bytes"
 "context"
 "crypto/tls"
 "encoding/base64"
 "encoding/binary"
 "encoding/json"
 "errors"
 "flag"
 "fmt"
 "io"
 "log"
 "math"
 "net/http"
 "os"
 "os/signal"
 "path/filepath"
 "runtime/debug"
 "strconv"
 "strings"
 "syscall"
 "time"
)

const version="0.2.2"
type Record struct {Topic string `json:"topic"`; Serial string `json:"serial"`; Resource string `json:"resource"`; Key string `json:"key"`; Config map[string]any `json:"config"`}
type Config struct {
 Host string `json:"mqtt_host"`; Port int `json:"mqtt_port"`; User string `json:"mqtt_user"`; Password string `json:"mqtt_password"`
 GatewayPassword string `json:"gateway_password"`; Prefix string `json:"topic_prefix"`; HAPrefix string `json:"ha_prefix"`
 Plan []Record `json:"plan"`; MowerIDs []string `json:"mower_ids"`
}
func(c Config)availability()string{return c.Prefix+"/local/availability"}
func readJSON(path string,target any,limit int64)error{f,e:=os.Open(path);if e!=nil{return e};defer f.Close();b,e:=io.ReadAll(io.LimitReader(f,limit+1));if e!=nil{return e};if int64(len(b))>limit{return errors.New("file too large")};return json.Unmarshal(b,target)}
func loadConfig(path string)(Config,error){
 var c Config;info,e:=os.Stat(path);if e!=nil{return c,e};if info.Mode().Perm()&0077!=0{return c,errors.New("configuration permissions")}
 if e=readJSON(path,&c,maxPacket);e!=nil{return c,e}
 if c.Host==""||c.Port<1||c.Port>65535||c.Prefix==""||c.HAPrefix==""||len(c.Plan)==0||len(c.Plan)>256||len(c.MowerIDs)>1{return c,errProtocol}
 for _,s:=range []string{c.Host,c.User,c.Password,c.GatewayPassword,c.Prefix,c.HAPrefix}{if len(s)>4096||strings.ContainsAny(s,"\x00\r\n"){return c,errProtocol}}
 if strings.ContainsAny(c.Prefix+c.HAPrefix,"+#"){return c,errProtocol}
 for _,r:=range c.Plan{state,ok:=r.Config["state_topic"].(string);if !ok||!strings.HasPrefix(state,c.Prefix+"/"+r.Key+"/")||!strings.HasSuffix(state,"/state")||!strings.HasPrefix(r.Topic,c.HAPrefix+"/")||strings.ContainsAny(r.Topic+state,"+#\x00"){return c,errProtocol}}
 return c,nil
}
func valueAt(data map[string]any,path ...string)any{var v any=data;for _,p:=range path{m,ok:=v.(map[string]any);if !ok{return nil};v=m[p]};return v}
func scalar(v any)string{switch x:=v.(type){case string:return x;case float64:return strconv.FormatFloat(x,'f',-1,64);default:return ""}}
func numeric(v any)(string,error){s:=scalar(v);n,e:=strconv.ParseFloat(s,64);if e!=nil||math.IsNaN(n)||math.IsInf(n,0){return "",errProtocol};return s,nil}
func canonical(s string)string{switch s{case "temperature":return "soil_temperature";case "mower_status":return "status";case "battery":return "battery_level"};return s}
func sensorValues(root string,plan []Record)([]string,error){
 // Read only the vendor's JSON files; never execute or modify vendor files.
 wanted:=map[string]bool{};for _,r:=range plan{wanted[r.Serial]=true}
 values:=map[string]map[string]string{};count:=0
 err:=filepath.WalkDir(root,func(path string,d os.DirEntry,e error)error{
  if e!=nil{return e};count++;if count>4096{return errors.New("too many sensor files")};if d.Type()&os.ModeSymlink!=0{return nil}
  if d.IsDir()||!strings.HasPrefix(d.Name(),"Device_descriptionID_")||!strings.HasSuffix(d.Name(),".json"){return nil}
  var desc map[string]any;if e=readJSON(path,&desc,65536);e!=nil{return e};serial,_:=desc["serialid"].(string);if !wanted[serial]{return nil}
  if values[serial]!=nil{return errors.New("duplicate device identity")};values[serial]=map[string]string{}
  parent:=filepath.Dir(path);entries,e:=os.ReadDir(filepath.Join(parent,"Value_description"));if e!=nil{return e};if len(entries)>1024{return errProtocol}
  for _,entry:=range entries{if entry.IsDir()||entry.Type()&os.ModeSymlink!=0||!strings.HasSuffix(entry.Name(),".json"){continue}
   var schema map[string]any;if e=readJSON(filepath.Join(parent,"Value_description",entry.Name()),&schema,65536);e!=nil{return e}
   name:=canonical(scalar(schema["name"]));needed:=false;for _,r:=range plan{if r.Serial==serial&&r.Resource==name{needed=true}};if !needed{continue}
   id:=scalar(schema["id"]);if id==""{id=strings.TrimSuffix(entry.Name(),".json");if i:=strings.LastIndex(id,"_");i>=0{id=id[i+1:]}}
   if _,e=strconv.ParseUint(id,10,32);e!=nil{return errProtocol}
   var value map[string]any;if e=readJSON(filepath.Join(parent,"Value","Value_"+id+"r.json"),&value,65536);e!=nil{return e}
   s,e:=numeric(value["value"]);if e!=nil{return e};values[serial][name]=s
  };return nil
 });if err!=nil{return nil,err}
 result:=make([]string,len(plan));for i,r:=range plan{v,ok:=values[r.Serial][r.Resource];if !ok{return nil,errors.New("missing sensor")};result[i]=v};return result,nil
}

func enableAPI(password string)error{
 transport:=&http.Transport{TLSClientConfig:&tls.Config{InsecureSkipVerify:true,MinVersion:tls.VersionTLS12}}
 defer transport.CloseIdleConnections()
 client:=&http.Client{Transport:transport,Timeout:15*time.Second,CheckRedirect:func(*http.Request,[]*http.Request)error{return errProtocol}}
 request:=func(method,path string,body any,session string)(map[string]any,error){
  b,_:=json.Marshal(body);req,e:=http.NewRequest(method,"https://127.0.0.1"+path,bytes.NewReader(b));if e!=nil{return nil,e};req.Header.Set("Content-Type","application/json");if session!=""{req.Header.Set("X-Session",session)}
  resp,e:=client.Do(req);if e!=nil{return nil,e};defer resp.Body.Close();if resp.StatusCode!=200&&resp.StatusCode!=204{return nil,errProtocol}
  raw,e:=io.ReadAll(io.LimitReader(resp.Body,65537));if e!=nil||len(raw)>65536{return nil,errProtocol};var out map[string]any;if len(raw)>0{e=json.Unmarshal(raw,&out)};return out,e
 }
 response,e:=request("POST","/login",map[string]any{"password":password},"");if e!=nil{return e};session,_:=response["session"].(string);if session==""{return errProtocol};_,e=request("PUT","/websocket_api",map[string]any{"enable":true},session);return e
}
type Mower struct{ID,Key,Name,Generation string;Data map[string]any}
type Message struct{Entity map[string]any `json:"entity"`;Op string `json:"op"`;Payload map[string]any `json:"payload"`;RequestID string `json:"request_id"`;Success bool `json:"success"`}
func models(number string)(string,string){switch number{case "488":return "GARDENA smart SILENO pro/max/free","gen2";case "6146":return "GARDENA smart SILENO","gen1";case "29694":return "GARDENA smart SILENO city/life","gen1";case "53988":return "GARDENA smart SILENO city/life LONA","gen1_lona"};return "",""}
func discover(w *websocket,c Config)([]Mower,error){
 pending:=map[string]bool{};var requests []map[string]any
 for _,service:=range []string{"lemonbeatd","lwm2mserver"}{id:=randomID();pending[id]=true;requests=append(requests,map[string]any{"entity":map[string]any{"service":service,"path":"devices"},"op":"read","request_id":id})}
 b,_:=json.Marshal(requests);if e:=w.send(1,b);e!=nil{return nil,e};devices:=map[string]any{}
 deadline:=time.Now().Add(35*time.Second)
 for len(pending)>0{if time.Now().After(deadline){return nil,errors.New("discovery timeout")};b,e:=w.receive();if e!=nil{return nil,e};var messages []Message;if json.Unmarshal(b,&messages)!=nil{return nil,errProtocol}
  for _,m:=range messages{if pending[m.RequestID]{delete(pending,m.RequestID);for k,v:=range m.Payload{devices[k]=v}}}
 }
 var mowers []Mower;for _,id:=range c.MowerIDs{data,ok:=devices[id].(map[string]any);if !ok{return nil,errors.New("mower missing")};name,generation:=models(scalar(valueAt(data,"device","0","model_number","vs")));if generation==""{return nil,errProtocol};mowers=append(mowers,Mower{id,c.Plan[0].Key,name,generation,data})};return mowers,nil
}
func (m Mower)activity()string{
 n:=scalar(valueAt(m.Data,"lemonbeat","0","status","vi"))
 if strings.HasPrefix(m.Generation,"gen1"){switch n{case "7","8","16","17","18","3":return "docked";case "1","4","15":return "mowing";case "2":return "returning";case "0","5","6","9","10","14":return "paused";case "12","13":return "error"};return ""}
 a:=scalar(valueAt(m.Data,"mower_app","0","activity","vi"));s:=scalar(valueAt(m.Data,"mower_app","0","state","vi"));switch a{case "1","5":return "docked";case "2","3":return "mowing";case "4":return "returning"};if s=="5"{return "paused"};if s=="3"||s=="8"{return "error"};return ""
}
func(m Mower)command(action string)(map[string]any,error){
 durations:=map[string]uint32{"start_mowing":28800,"start_1h":3600,"start_3h":10800,"start_6h":21600};duration,start:=durations[action]
 entity:=map[string]any{"device":m.ID};request:=map[string]any{"entity":entity,"request_id":randomID()}
 if m.Generation=="gen2"{entity["service"]="lwm2mserver";request["op"]="execute";switch{case start:entity["path"]="smart_system_mower_api/0/manual_start";request["payload"]=map[string]any{"as":[]string{fmt.Sprintf("0='%d'",duration)}};case action=="dock":entity["path"]="smart_system_mower_api/0/park_until_further_notice";case action=="pause":entity["path"]="mower_app/0/pause";default:return nil,errProtocol}}else{
  entity["service"]="lemonbeatd";request["op"]="write"
  switch{case start&&m.Generation=="gen1_lona":entity["path"]="lemonbeat/0/mower_timer_with_distance";b:=make([]byte,6);binary.BigEndian.PutUint32(b[2:],duration);request["payload"]=map[string]any{"vo":base64.StdEncoding.EncodeToString(b)}
  case start:entity["path"]="lemonbeat/0/mower_timer";request["payload"]=map[string]any{"vi":duration}
  case action=="dock":entity["path"]="lemonbeat/0/action_paused_until_1";b:=[]byte{0,0,12,31,22,0};binary.LittleEndian.PutUint16(b,2042);request["payload"]=map[string]any{"vo":base64.StdEncoding.EncodeToString(b)}
  default:return nil,errProtocol}
 };return request,nil
}
func apply(m *Mower,msg Message){if msg.Entity["device"]!=m.ID||(msg.Op!="update"&&msg.Op!="overwrite"){return};path,ok:=msg.Entity["path"].(string);if !ok||path==""{return};target:=m.Data;for _,part:=range strings.Split(path,"/"){next,ok:=target[part].(map[string]any);if !ok{next=map[string]any{};target[part]=next};target=next};for k,v:=range msg.Payload{target[k]=v}}
func publishJSON(m *mqttClient,topic string,payload any)error{b,e:=json.Marshal(payload);if e!=nil{return e};return m.publish(topic,string(b),true)}
func publishAll(m *mqttClient,c Config,mowers []Mower,root string)error{
 values,e:=measuredSensorValues(root,c.Plan);if e!=nil{return e}
 for i,r:=range c.Plan{cfg:=map[string]any{};for k,v:=range r.Config{cfg[k]=v};cfg["availability_topic"]=c.availability();cfg["payload_available"]="online";cfg["payload_not_available"]="offline"
  if e=publishJSON(m,r.Topic,cfg);e!=nil{return e};if e=m.publish(r.Config["state_topic"].(string),values[i],true);e!=nil{return e}}
 for _,mower:=range mowers{base:=c.Prefix+"/"+mower.Key+"/mower";device:=map[string]any{"identifiers":[]string{"gardena_"+mower.Key},"name":mower.Name,"manufacturer":"GARDENA","model":mower.Name}
  cfg:=map[string]any{"name":nil,"unique_id":"gardena_"+mower.Key+"_lawn_mower","activity_state_topic":base+"/activity/state","start_mowing_command_topic":base+"/command","dock_command_topic":base+"/command","availability_topic":c.availability(),"device":device}
  if mower.Generation=="gen2"{cfg["pause_command_topic"]=base+"/command"};if e=publishJSON(m,c.HAPrefix+"/lawn_mower/gardena_"+mower.Key+"/config",cfg);e!=nil{return e}
  for _,hours:=range []int{1,3,6}{id:=fmt.Sprintf("gardena_%s_start_%dh",mower.Key,hours);cfg:=map[string]any{"unique_id":id,"object_id":id,"name":fmt.Sprintf("Start %d h",hours),"command_topic":base+"/command","payload_press":fmt.Sprintf("start_%dh",hours),"availability_topic":c.availability(),"device":device,"icon":"mdi:timer-play-outline"};if e=publishJSON(m,fmt.Sprintf("%s/button/gardena_%s/start_%dh/config",c.HAPrefix,mower.Key,hours),cfg);e!=nil{return e}}
  if a:=mower.activity();a!=""{if e=m.publish(base+"/activity/state",a,true);e!=nil{return e}}
 };return m.publish(c.availability(),"online",true)
}
func status(ready bool){b,_:=json.Marshal(map[string]any{"ready":ready,"version":version,"updated":time.Now().Unix()});_ = os.WriteFile("/run/gardena-local-status.json.tmp",b,0600);_ = os.Rename("/run/gardena-local-status.json.tmp","/run/gardena-local-status.json")}
var prepareLocalAPI = enableAPI
var openLocalWS = connectWS
var pollInterval = 30*time.Second
var heartbeatInterval = 15*time.Second
func session(ctx context.Context,c Config,root string)error{
 diagnostics.attempt();diagnostics.stage("api_enable")
 if e:=prepareLocalAPI(c.GatewayPassword);e!=nil{return e};diagnostics.stage("ws_connect");w,e:=openLocalWS(c.GatewayPassword);if e!=nil{return e};defer w.Close()
 diagnostics.stage("ws_discover");mowers,e:=discover(w,c);if e!=nil{return e};diagnostics.stage("mqtt_connect");m,e:=connectMQTT(c);if e!=nil{return e};defer m.Close()
 defer func(){status(false);_ = m.publish(c.availability(),"offline",true)}()
 diagnostics.stage("mqtt_subscribe");if e=m.subscribe(c.Prefix+"/+/mower/command",1);e!=nil{return e}
 // Require successful SUBACK before reporting control readiness.
 for{p,e:=m.receive();if e!=nil{return e};if p.header==0x90{if len(p.body)!=3||p.body[0]!=0||p.body[1]!=1||p.body[2]!=0{return errProtocol};break}}
 if e=publishAll(m,c,mowers,root);e!=nil{return e};status(true);log.Print("Gateway MQTT ready; installer not required")
 packets:=make(chan mqttPacket,16);events:=make(chan []byte,16);fail:=make(chan error,2);done:=make(chan struct{});defer close(done)
 go func(){for{p,e:=m.receive();if e!=nil{fail<-atStage("mqtt_read",e);return};select{case packets<-p:case <-done:return}}}()
 go func(){for{b,e:=w.receive();if e!=nil{fail<-atStage("ws_read",e);return};select{case events<-b:case <-done:return}}}()
 poll:=time.NewTicker(pollInterval);defer poll.Stop();heartbeat:=time.NewTicker(heartbeatInterval);defer heartbeat.Stop()
 pending:=map[string]time.Time{};lastHeartbeat:=time.Now()
 diagnostics.stage("event_loop")
 for{select{
 case <-ctx.Done():return nil
 case e:=<-fail:return e
 case <-poll.C:if e=publishAll(m,c,mowers,root);e!=nil{return e};status(true);diagnostics.stage("event_loop")
 case <-heartbeat.C:diagnostics.heartbeat(time.Since(lastHeartbeat));lastHeartbeat=time.Now();diagnostics.stage("heartbeat");if e=m.send(0xc0,nil);e!=nil{return e};if e=w.send(9,nil);e!=nil{return atStage("ws_write",e)};diagnostics.stage("event_loop");for id,t:=range pending{if time.Now().After(t){delete(pending,id);log.Print("Command acknowledgement timeout")}}
 case p:=<-packets:if p.header>>4!=3{continue};topic,action,retained,e:=parsePublish(p);if e!=nil{return e};if retained||len(pending)>=32{continue}
  for _,mower:=range mowers{if topic!=c.Prefix+"/"+mower.Key+"/mower/command"{continue};command,e:=mower.command(strings.TrimSpace(action));if e!=nil{continue};b,_:=json.Marshal([]any{command});if e=w.send(1,b);e!=nil{return atStage("ws_write",e)};pending[command["request_id"].(string)]=time.Now().Add(30*time.Second)}
 case b:=<-events:var messages []Message;if json.Unmarshal(b,&messages)!=nil{continue};for _,msg:=range messages{if _,ok:=pending[msg.RequestID];ok{delete(pending,msg.RequestID);log.Printf("Command acknowledged: %t",msg.Success)};for i:=range mowers{apply(&mowers[i],msg);if a:=mowers[i].activity();a!=""{if e=m.publish(c.Prefix+"/"+mowers[i].Key+"/mower/activity/state",a,true);e!=nil{return e}}}}
 }}
}
func run(ctx context.Context,c Config,root string,retry time.Duration){
 for ctx.Err()==nil{status(false);if e:=session(ctx,c,root);e!=nil{diagnostics.failure(e)};diagnostics.stage("retry_wait");select{case <-ctx.Done():return;case <-time.After(retry):}}
}
func main(){
 path:=flag.String("config","/etc/gardena-local/config.json","configuration file");check:=flag.Bool("check",false,"validate configuration, sensor data and local API without publishing");showVersion:=flag.Bool("version",false,"print version");flag.Parse();if *showVersion{fmt.Println(version);return}
 debug.SetMemoryLimit(32*1024*1024);debug.SetGCPercent(50)
 c,e:=loadConfig(*path);if e!=nil{log.Print("Configuration invalid");os.Exit(2)}
 root:="/var/lib/lemonbeatd"
 if *check{if _,e=sensorValues(root,c.Plan);e==nil{e=enableAPI(c.GatewayPassword)};if e==nil{var w *websocket;w,e=connectWS(c.GatewayPassword);if e==nil{_,e=discover(w,c);w.Close()}};if e!=nil{log.Print("Gateway preflight failed");os.Exit(1)};fmt.Println("preflight-ok");return}
 ctx,stop:=signal.NotifyContext(context.Background(),os.Interrupt,syscall.SIGTERM);defer stop()
 go diagnostics.record(ctx,"/run/gardena-local-diagnostics.json",5*time.Second)
 run(ctx,c,root,15*time.Second)
}
