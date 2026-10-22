package org.lira.receiver;
import org.json.*;import java.nio.file.*;import java.io.*;import java.security.*;
public final class DictationManualNavigationTest {
 static class Sound extends DictationEngineTest.Sound{int stops;@Override public void stop(){stops++;}}
 static void check(boolean b,String message){if(!b)throw new AssertionError(message);}
 public static void main(String[] args)throws Exception{
  JSONObject task=new JSONObject(new String(Files.readAllBytes(Paths.get(args[0])),"UTF-8"));
  task.getJSONObject("playback").put("manual_navigation",true).put("repeat_count",1);
  JSONArray items=task.getJSONArray("phases").getJSONObject(1).getJSONArray("items");
  items.put(new JSONObject(items.getJSONObject(0).toString()).put("item_id","dictate-2").put("text","晴朗"));
  byte[] pcm={1,0};StringBuilder sha=new StringBuilder();for(byte b:MessageDigest.getInstance("SHA-256").digest(pcm))sha.append(String.format("%02x",b&255));
  task.getJSONArray("audio_assets").getJSONObject(0).put("sha256",sha.toString());task.put("content_hash",DictationSpec.contentHash(task));DictationSpec.parse(task);
  File root=Files.createTempDirectory("manual-navigation").toFile();TaskAssetStore assets=new TaskAssetStore(new File(root,"assets"));String tid=task.getString("task_id"),hash=task.getString("content_hash");assets.begin(tid,hash,"audio-2",2,sha.toString());assets.append(tid,hash,"audio-2",0,pcm);assets.commit(tid,hash,"audio-2");
  JSONObject manifest=new JSONObject().put("task_id",tid).put("content_hash",hash).put("assets",new JSONArray().put(new JSONObject().put("asset_id","audio-2").put("sha256",sha.toString()).put("pcm_bytes",2).put("frames",1).put("sample_rate",24000).put("channels",1)));
  DictationEngineTest.Time clock=new DictationEngineTest.Time();Sound sound=new Sound();DictationEngine engine=new DictationEngine(clock,sound,(k,w,t)->{},new DictationStateStore(new File(root,"state")),assets);
  engine.start(new JSONObject().put("task",task).put("manifest",manifest).put("run_epoch",1));engine.control(DictationEngineV2Test.cmd(engine,"start"));engine.control(DictationEngineV2Test.cmd(engine,"confirm"));sound.drain();
  check(engine.status().getString("state").equals("waiting_child"),"one playback then stop");clock.t+=5000;engine.lease(DictationEngineV2Test.scope(engine).put("lease_ms",10000));engine.tick();check(engine.status().getInt("index")==1&&engine.status().getInt("actual_repeats")==1,"no timed replay or advance");
  engine.control(DictationEngineV2Test.cmd(engine,"replay"));sound.drain();check(engine.status().getInt("index")==1,"center replays current");
  engine.control(DictationEngineV2Test.cmd(engine,"confirm"));sound.drain();check(engine.status().getInt("index")==2,"right advances and plays");
  JSONObject previous=DictationEngineV2Test.cmd(engine,"previous");engine.control(previous);sound.drain();check(engine.status().getInt("index")==1,"left returns and plays");engine.control(previous);check(engine.status().getInt("index")==1,"duplicate left cannot navigate twice");
  engine.control(DictationEngineV2Test.cmd(engine,"previous"));sound.drain();check(engine.status().getInt("index")==1,"first word does not wrap into recognition");
  engine.control(DictationEngineV2Test.cmd(engine,"replay"));DictationEngine.Completion stale=sound.done;
  engine.control(DictationEngineV2Test.cmd(engine,"confirm"));stale.done(true);check(engine.status().getInt("index")==2&&engine.status().getString("state").equals("playing"),"old playback callback cannot finish new item");sound.drain();
  engine.control(DictationEngineV2Test.cmd(engine,"replay"));DictationEngine.Completion oldReplay=sound.done;int stopped=sound.stops;
  engine.control(DictationEngineV2Test.cmd(engine,"replay"));oldReplay.done(true);check(sound.stops>stopped&&engine.status().getString("state").equals("playing"),"replay stops audio and ignores stale callback");
  DictationEngine.Completion oldPrevious=sound.done;stopped=sound.stops;engine.control(DictationEngineV2Test.cmd(engine,"previous"));oldPrevious.done(true);check(sound.stops>stopped&&engine.status().getInt("index")==1&&engine.status().getString("state").equals("playing"),"previous stops audio and ignores stale callback");sound.drain();
  engine.control(DictationEngineV2Test.cmd(engine,"pause"));try{engine.control(DictationEngineV2Test.cmd(engine,"previous"));throw new AssertionError("paused previous bypassed gate");}catch(IllegalArgumentException expected){}
  engine.control(DictationEngineV2Test.cmd(engine,"resume"));sound.drain();
  engine.control(DictationEngineV2Test.cmd(engine,"confirm"));sound.drain();engine.control(DictationEngineV2Test.cmd(engine,"confirm"));check(engine.status().getString("state").equals("completed"),"right after final word completes once");
  check(assets.read(tid,hash,"audio-2").exists(),"retain audio until terminal events acknowledged");
  for(int n=0;n<3;n++)assets.begin("historical-"+n,hash,"audio-2",2,sha.toString());
  try{assets.begin("fifth-task",hash,"audio-2",2,sha.toString());throw new AssertionError("quota precondition missing");}catch(IOException expected){}
  JSONObject terminal=engine.status();engine.ack(DictationEngineV2Test.scope(engine).put("through_sequence",terminal.getLong("sequence")));
  try{assets.read(tid,hash,"audio-2");throw new AssertionError("terminal acknowledged task must release audio cache before fifth task upload");}catch(IOException expected){}
  assets.begin("fifth-task",hash,"audio-2",2,sha.toString());
  System.out.println("manual: one play, wait, replay, previous, next, bounds, idempotency and completion passed");
 }
}
