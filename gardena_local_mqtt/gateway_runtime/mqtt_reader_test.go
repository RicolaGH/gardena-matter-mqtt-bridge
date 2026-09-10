package main

import (
    "bufio"
    "bytes"
    "errors"
    "io"
    "net"
    "sync"
    "testing"
    "time"
)

func readerClient(conn net.Conn)*mqttClient{return &mqttClient{Conn:conn,reader:bufio.NewReader(conn)}}
func wirePacket(header byte,body []byte)[]byte{
    packet:=[]byte{header};n:=len(body)
    for{digit:=byte(n%128);n/=128;if n>0{digit|=128};packet=append(packet,digit);if n==0{break}}
    return append(packet,body...)
}

func TestMQTTReaderDelayedAndSplitPong(t *testing.T){
    a,b:=net.Pipe();defer a.Close();defer b.Close()
    done:=make(chan struct{});go func(){defer close(done);time.Sleep(25*time.Millisecond);b.Write([]byte{0xd0});time.Sleep(25*time.Millisecond);b.Write([]byte{0})}()
    packet,e:=readerClient(a).receive();if e!=nil||packet.header!=0xd0||len(packet.body)!=0{t.Fatalf("split PONG: %v %+v",e,packet)}
    <-done
}

func TestMQTTReaderCoalescedPacketsAndSplitRemainingLength(t *testing.T){
    body:=append(mqttString("gardena/test/mower/command"),bytes.Repeat([]byte("x"),300)...)
    publish:=wirePacket(0x30,body)
    stream:=append([]byte{0xd0,0},publish...);stream=append(stream,0xd0,0,0xd0,0)
    for _,cut:=range []int{1,2,3,4,5,6,10,64,127,128,129,len(stream)-1}{
        a,b:=net.Pipe();done:=make(chan struct{})
        go func(){defer close(done);defer b.Close();b.Write(stream[:cut]);b.Write(stream[cut:])}()
        client:=readerClient(a)
        for i,header:=range []byte{0xd0,0x30,0xd0,0xd0}{
            p,e:=client.receive();if e!=nil||p.header!=header{a.Close();t.Fatalf("cut=%d packet=%d error=%v header=%x",cut,i,e,p.header)}
            if i==1&&!bytes.Equal(p.body,body){a.Close();t.Fatal("PUBLISH body corrupted")}
        }
        _,e:=client.receive();if !errors.Is(e,io.EOF){t.Fatalf("expected EOF, got %v",e)}
        a.Close();<-done
    }
}

// Shrink real socket deadlines only in tests; production remains 90s idle / 15s packet.
type shortDeadlineConn struct{net.Conn;mu sync.Mutex;requested []time.Duration}
func(c *shortDeadlineConn)SetReadDeadline(deadline time.Time)error{
    requested:=time.Until(deadline);c.mu.Lock();c.requested=append(c.requested,requested);c.mu.Unlock()
    return c.Conn.SetReadDeadline(time.Now().Add(100*time.Millisecond))
}
func TestMQTTReaderTimeoutWithoutReplyAndDuringPartialReply(t *testing.T){
    for _,partial:=range []bool{false,true}{
        a,b:=net.Pipe();wrapped:=&shortDeadlineConn{Conn:a}
        done:=make(chan struct{});go func(){defer close(done);if partial{b.Write([]byte{0xd0})}}()
        _,e:=readerClient(wrapped).receive();var timeout net.Error
        if !errors.As(e,&timeout)||!timeout.Timeout(){t.Fatalf("partial=%t expected timeout, got %v",partial,e)}
        expected:=1;if partial{expected=2}
        if len(wrapped.requested)!=expected{t.Fatalf("wrong deadline transitions: %v",wrapped.requested)}
        if wrapped.requested[0]<89*time.Second||wrapped.requested[0]>90*time.Second{t.Fatal("idle deadline changed")}
        if partial&&(wrapped.requested[1]<14*time.Second||wrapped.requested[1]>15*time.Second){t.Fatal("packet deadline changed")}
        a.Close();b.Close();<-done
    }
}

func TestMQTTReaderPongsDoNotResetIncompletePacketDeadline(t *testing.T){
    // A PONG cannot be interleaved inside an unfinished MQTT packet: it would be payload.
    // Once the first packet byte arrives, the shorter whole-packet deadline is deliberate.
    a,b:=net.Pipe();defer a.Close();defer b.Close()
    wrapped:=&shortDeadlineConn{Conn:a};done:=make(chan struct{})
    go func(){defer close(done);b.Write([]byte{0x30,0x7f,0,1,'t'});for i:=0;i<4;i++{time.Sleep(15*time.Millisecond);if _,e:=b.Write([]byte{'x'});e!=nil{return}}}()
    _,e:=readerClient(wrapped).receive();var timeout net.Error
    if !errors.As(e,&timeout)||!timeout.Timeout(){t.Fatal("incomplete packet did not time out")}
    if len(wrapped.requested)!=2{t.Fatal("packet deadline was reset by partial data")}
    a.Close();<-done
}

func TestMQTTReaderConcurrentPingPongOverTCP(t *testing.T){
    listener,e:=net.Listen("tcp","127.0.0.1:0");if e!=nil{t.Fatal(e)};defer listener.Close()
    const count=120
    serverDone:=make(chan error,1)
    go func(){conn,e:=listener.Accept();if e!=nil{serverDone<-e;return};defer conn.Close();conn.SetDeadline(time.Now().Add(5*time.Second))
        for i:=0;i<count;i++{var packet [2]byte;if _,e=io.ReadFull(conn,packet[:]);e!=nil{serverDone<-e;return};if packet!=[2]byte{0xc0,0}{serverDone<-errProtocol;return}
            if i%2==0{_,e=conn.Write([]byte{0xd0,0})}else{_,e=conn.Write([]byte{0xd0});if e==nil{_,e=conn.Write([]byte{0})}}
            if e!=nil{serverDone<-e;return}
        };serverDone<-nil
    }()
    conn,e:=net.DialTimeout("tcp",listener.Addr().String(),time.Second);if e!=nil{t.Fatal(e)};defer conn.Close()
    client:=readerClient(conn);sent:=make(chan error,1)
    go func(){for i:=0;i<count;i++{if e:=client.send(0xc0,nil);e!=nil{sent<-e;return}};sent<-nil}()
    for i:=0;i<count;i++{p,e:=client.receive();if e!=nil||p.header!=0xd0||len(p.body)!=0{t.Fatalf("PONG %d: %v",i,e)}}
    if e=<-sent;e!=nil{t.Fatal(e)};if e=<-serverDone;e!=nil{t.Fatal(e)}
}
