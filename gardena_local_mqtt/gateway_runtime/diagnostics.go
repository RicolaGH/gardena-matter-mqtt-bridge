package main

import (
    "context"
    "encoding/json"
    "errors"
    "io"
    "log"
    "net"
    "os"
    "runtime"
    "sync"
    "time"
)

// No addresses, device identifiers, payloads, file paths or raw errors are recorded.
// Bounded history stays in /run (RAM); one small atomic snapshot every five seconds.
type diagnosticEvent struct {
    Time int64 `json:"time"`
    Stage string `json:"stage"`
    Kind string `json:"kind"`
}
type diagnosticSnapshot struct {
    Version string `json:"version"`
    Updated int64 `json:"updated"`
    Started int64 `json:"started"`
    Stage string `json:"stage"`
    StageMS int64 `json:"stage_ms"`
    Attempts uint64 `json:"attempts"`
    MQTTTx int64 `json:"mqtt_tx"`
    MQTTRx int64 `json:"mqtt_rx"`
    Pings uint64 `json:"pings"`
    Pongs uint64 `json:"pongs"`
    WSRx int64 `json:"ws_rx"`
    SensorMS int64 `json:"sensor_ms"`
    SensorFailures uint64 `json:"sensor_failures"`
    HeartbeatMS int64 `json:"heartbeat_ms"`
    HeapBytes uint64 `json:"heap_bytes"`
    Goroutines int `json:"goroutines"`
    Events []diagnosticEvent `json:"events"`
}
type diagnosticState struct {
    mu sync.Mutex
    stageSince time.Time
    state diagnosticSnapshot
}
func newDiagnostics()*diagnosticState {
    now:=time.Now()
    return &diagnosticState{stageSince:now,state:diagnosticSnapshot{Version:version,Started:now.Unix(),Stage:"starting",Events:[]diagnosticEvent{}}}
}
var diagnostics = newDiagnostics()
func(d *diagnosticState) stage(stage string){d.mu.Lock();defer d.mu.Unlock();d.state.Stage=stage;d.stageSince=time.Now()}
func(d *diagnosticState) attempt(){d.mu.Lock();defer d.mu.Unlock();d.state.Attempts++}
func(d *diagnosticState) tx(header byte){d.mu.Lock();defer d.mu.Unlock();d.state.MQTTTx=time.Now().Unix();if header==0xc0{d.state.Pings++}}
func(d *diagnosticState) rx(header byte){d.mu.Lock();defer d.mu.Unlock();d.state.MQTTRx=time.Now().Unix();if header==0xd0{d.state.Pongs++}}
func(d *diagnosticState) ws(){d.mu.Lock();defer d.mu.Unlock();d.state.WSRx=time.Now().Unix()}
func(d *diagnosticState) sensors(elapsed time.Duration,err error){d.mu.Lock();defer d.mu.Unlock();d.state.SensorMS=elapsed.Milliseconds();if err!=nil{d.state.SensorFailures++}}
func(d *diagnosticState) heartbeat(elapsed time.Duration){d.mu.Lock();defer d.mu.Unlock();d.state.HeartbeatMS=elapsed.Milliseconds()}
func errorKind(err error)string {
    var network net.Error
    switch {
    case errors.As(err,&network)&&network.Timeout():return "timeout"
    case errors.Is(err,io.EOF)||errors.Is(err,io.ErrUnexpectedEOF):return "connection_closed"
    case errors.Is(err,errProtocol):return "protocol"
    case errors.Is(err,os.ErrNotExist):return "file_missing"
    default:return "error"
    }
}
type stageError struct { stage string; err error }
func(e stageError)Error()string{return e.stage}
func(e stageError)Unwrap()error{return e.err}
func atStage(stage string,err error)error{if err==nil{return nil};return stageError{stage,err}}
func(d *diagnosticState) failure(err error){
    d.mu.Lock();defer d.mu.Unlock()
    stage:=d.state.Stage;var wrapped stageError;if errors.As(err,&wrapped){stage=wrapped.stage}
    event:=diagnosticEvent{time.Now().Unix(),stage,errorKind(err)}
    if len(d.state.Events)==32{copy(d.state.Events,d.state.Events[1:]);d.state.Events=d.state.Events[:31]}
    d.state.Events=append(d.state.Events,event)
    log.Printf("Connection unavailable: stage=%s kind=%s; retrying",event.Stage,event.Kind)
}
func(d *diagnosticState) snapshot()diagnosticSnapshot {
    var mem runtime.MemStats;runtime.ReadMemStats(&mem)
    d.mu.Lock();defer d.mu.Unlock()
    s:=d.state;s.Updated=time.Now().Unix();s.StageMS=time.Since(d.stageSince).Milliseconds()
    s.HeapBytes=mem.HeapAlloc;s.Goroutines=runtime.NumGoroutine();s.Events=append([]diagnosticEvent{},s.Events...)
    return s
}
func(d *diagnosticState) write(path string)error {
    b,e:=json.Marshal(d.snapshot());if e!=nil{return e}
    // Only one writer per file. Fixed path in a root-owned runtime directory.
    if e=os.WriteFile(path+".tmp",b,0600);e!=nil{return e};return os.Rename(path+".tmp",path)
}
func(d *diagnosticState) record(ctx context.Context,path string,interval time.Duration){
    tick:=time.NewTicker(interval);defer tick.Stop()
    for{_ = d.write(path);select{case <-ctx.Done():return;case <-tick.C:}}
}

var readSensorValues = sensorValues
func measuredSensorValues(root string,plan []Record)([]string,error){
    diagnostics.stage("sensor_read");started:=time.Now()
    values,e:=readSensorValues(root,plan);elapsed:=time.Since(started);diagnostics.sensors(elapsed,e)
    if e!=nil{return nil,atStage("sensor_read",e)}
    if elapsed>5*time.Second{log.Printf("Slow sensor read: duration_ms=%d",elapsed.Milliseconds())}
    diagnostics.stage("mqtt_publish");return values,nil
}
