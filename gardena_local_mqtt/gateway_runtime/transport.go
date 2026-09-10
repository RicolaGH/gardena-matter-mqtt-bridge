// Gateway-native transport. No Home Assistant connection or SSH tunnel is used.
package main

import (
 "bufio"
 "bytes"
 "crypto/rand"
 "crypto/sha1"
 "crypto/tls"
 "encoding/base64"
 "encoding/binary"
 "errors"
 "fmt"
 "io"
 "net"
 "net/http"
 "strings"
 "sync"
 "time"
)

const maxPacket = 256*1024
var errProtocol = errors.New("invalid protocol data")

type mqttPacket struct { header byte; body []byte }
type mqttClient struct { net.Conn; reader *bufio.Reader; mu sync.Mutex }
func mqttString(s string) []byte { b:=make([]byte,2+len(s)); binary.BigEndian.PutUint16(b,uint16(len(s))); copy(b[2:],s); return b }
func (m *mqttClient) send(header byte, body []byte) error {
 if len(body)>maxPacket { return errProtocol }; m.mu.Lock(); defer m.mu.Unlock()
 packet:=[]byte{header}; n:=len(body)
 for { digit:=byte(n%128); n/=128; if n>0 { digit|=128 }; packet=append(packet,digit); if n==0 {break} }
 packet=append(packet,body...); m.SetWriteDeadline(time.Now().Add(10*time.Second))
 _,err:=io.Copy(m.Conn,bytes.NewReader(packet)); if err==nil{diagnostics.tx(header)};return atStage("mqtt_write",err)
}
func (m *mqttClient) receive() (mqttPacket,error) {
 m.SetReadDeadline(time.Now().Add(90*time.Second)); first,err:=m.reader.ReadByte(); if err!=nil{return mqttPacket{},err}
 m.SetReadDeadline(time.Now().Add(15*time.Second)); n,mult:=0,1
 for i:=0;i<4;i++ { b,e:=m.reader.ReadByte(); if e!=nil{return mqttPacket{},e}; n+=int(b&127)*mult
  if n>maxPacket{return mqttPacket{},errProtocol}; if b&128==0 {body:=make([]byte,n); _,e=io.ReadFull(m.reader,body);if e==nil{diagnostics.rx(first)}; return mqttPacket{first,body},e}; mult*=128 }
 return mqttPacket{},errProtocol
}
func (m *mqttClient) publish(topic,payload string,retain bool) error { h:=byte(0x30); if retain{h|=1}; return m.send(h,append(mqttString(topic),[]byte(payload)...)) }
func (m *mqttClient) subscribe(topic string,id uint16) error {b:=[]byte{byte(id>>8),byte(id)}; b=append(b,mqttString(topic)...); return m.send(0x82,append(b,0))}
func connectMQTT(c Config) (*mqttClient,error) {
 conn,err:=net.DialTimeout("tcp",net.JoinHostPort(c.Host,fmt.Sprint(c.Port)),10*time.Second); if err!=nil{return nil,err}
 m:=&mqttClient{Conn:conn,reader:bufio.NewReader(conn)}
 flags:=byte(0x26); payload:=mqttString("gardena-gateway-"+randomID())
 payload=append(payload,mqttString(c.availability())...);payload=append(payload,mqttString("offline")...)
 if c.User!=""{flags|=0x80;payload=append(payload,mqttString(c.User)...)}
 if c.Password!=""{flags|=0x40;payload=append(payload,mqttString(c.Password)...)}
 variable:=append(mqttString("MQTT"),4,flags,0,60)
 if err=m.send(0x10,append(variable,payload...));err==nil{var p mqttPacket;p,err=m.receive();if err==nil&&(p.header!=0x20||len(p.body)!=2||p.body[1]!=0){err=errProtocol}}
 if err!=nil{conn.Close();return nil,err};return m,nil
}
func parsePublish(p mqttPacket)(string,string,bool,error){
 if p.header>>4!=3||p.header&6!=0||len(p.body)<2{return "","",false,errProtocol}
 n:=int(binary.BigEndian.Uint16(p.body[:2])); if n==0||n+2>len(p.body){return "","",false,errProtocol}
 topic:=string(p.body[2:2+n]);if strings.ContainsAny(topic,"#+\x00"){return "","",false,errProtocol}
 return topic,string(p.body[2+n:]),p.header&1!=0,nil
}
func randomID()string{var b [12]byte;if _,err:=rand.Read(b[:]);err!=nil{panic("random source unavailable")};return fmt.Sprintf("%x",b[:])}

type websocket struct {net.Conn; mu sync.Mutex}
func connectWS(password string)(*websocket,error){
 return dialWS("127.0.0.1:8443",password)
}
func dialWS(address,password string)(*websocket,error){
 // Self-signed certificate on gateway loopback. No externally configurable WS URL.
 conn,err:=tls.DialWithDialer(&net.Dialer{Timeout:10*time.Second},"tcp",address,&tls.Config{InsecureSkipVerify:true,MinVersion:tls.VersionTLS12})
 if err!=nil{return nil,err}; fail:=func(e error)(*websocket,error){conn.Close();return nil,e}
 key:=base64.StdEncoding.EncodeToString([]byte(randomID()[:16])); auth:=base64.StdEncoding.EncodeToString([]byte("_:"+password))
 conn.SetDeadline(time.Now().Add(15*time.Second))
 request:="GET / HTTP/1.1\r\nHost: "+address+"\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: "+key+"\r\nAuthorization: Basic "+auth+"\r\n\r\n"
 if _,err=io.WriteString(conn,request);err!=nil{return fail(err)}
 var header []byte; one:=make([]byte,1)
 for !bytes.HasSuffix(header,[]byte("\r\n\r\n")){if len(header)>=16384{return fail(errProtocol)};if _,err=io.ReadFull(conn,one);err!=nil{return fail(err)};header=append(header,one[0])}
 response,err:=http.ReadResponse(bufio.NewReader(bytes.NewReader(header)),nil);if err!=nil{return fail(err)}
 expected:=sha1.Sum([]byte(key+"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
 if response.StatusCode!=101||!strings.EqualFold(response.Header.Get("Upgrade"),"websocket")||!hasToken(response.Header.Get("Connection"),"upgrade")||response.Header.Get("Sec-WebSocket-Accept")!=base64.StdEncoding.EncodeToString(expected[:]){return fail(errProtocol)}
 conn.SetDeadline(time.Time{});return &websocket{Conn:conn},nil
}
func hasToken(value,token string)bool{for _,s:=range strings.Split(value,","){if strings.EqualFold(strings.TrimSpace(s),token){return true}};return false}
func (w *websocket) send(op byte,payload []byte)error{
 if len(payload)>maxPacket{return errProtocol};w.mu.Lock();defer w.mu.Unlock()
 header:=[]byte{0x80|op};n:=len(payload)
 if n<126{header=append(header,0x80|byte(n))}else if n<65536{header=append(header,0xfe,byte(n>>8),byte(n))}else{header=append(header,0xff,0,0,0,0,byte(n>>24),byte(n>>16),byte(n>>8),byte(n))}
 var mask [4]byte;if _,err:=rand.Read(mask[:]);err!=nil{return err};header=append(header,mask[:]...)
 for i,b:=range payload{header=append(header,b^mask[i%4])};w.SetWriteDeadline(time.Now().Add(10*time.Second));_,err:=io.Copy(w.Conn,bytes.NewReader(header));return err
}
func(w *websocket) receive()([]byte,error){
 for{w.SetReadDeadline(time.Now().Add(90*time.Second));var h [2]byte;if _,err:=io.ReadFull(w.Conn,h[:]);err!=nil{return nil,err}
 w.SetReadDeadline(time.Now().Add(15*time.Second));if h[0]&0x80==0||h[0]&0x70!=0||h[1]&0x80!=0{return nil,errProtocol}
 n:=uint64(h[1]&127);if n==126{var b [2]byte;if _,err:=io.ReadFull(w.Conn,b[:]);err!=nil{return nil,err};n=uint64(binary.BigEndian.Uint16(b[:]))}else if n==127{var b [8]byte;if _,err:=io.ReadFull(w.Conn,b[:]);err!=nil{return nil,err};n=binary.BigEndian.Uint64(b[:])}
 op:=h[0]&15;if n>maxPacket||(op>=8&&n>125){return nil,errProtocol};body:=make([]byte,int(n));if _,err:=io.ReadFull(w.Conn,body);err!=nil{return nil,err}
 diagnostics.ws();switch op{case 1:return body,nil;case 8:return nil,io.EOF;case 9:if err:=w.send(10,body);err!=nil{return nil,err};case 10:default:return nil,errProtocol}
 }
}
