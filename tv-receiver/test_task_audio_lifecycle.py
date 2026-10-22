"""Exercise the real Android adapter against a delayed speaker pipeline.

The fake mixer reports its playback head before its downstream speaker tail,
which is the distinction the earlier predicate-only test did not exercise.
"""
from pathlib import Path
import os,shutil,subprocess
import pytest

ROOT=Path(__file__).parent

@pytest.mark.parametrize('mode',['complete','cancel'])
def test_short_word_survives_startup_and_speaker_tail(tmp_path,mode):
    javac=shutil.which('javac'); java=shutil.which('java')
    assert javac and java, 'JDK required for native adapter lifecycle regression'
    sources={
      'android/content/Context.java':'''package android.content; public class Context {public static final String AUDIO_SERVICE="audio";public Object getSystemService(String key){return new android.media.AudioManager();}}''',
      'android/media/AudioDeviceInfo.java':'''package android.media;public class AudioDeviceInfo{public static final int TYPE_BUILTIN_SPEAKER=2;public int getType(){return 2;}}''',
      'android/media/AudioFormat.java':'''package android.media;public class AudioFormat{public static final int CHANNEL_OUT_MONO=4,ENCODING_PCM_16BIT=2;}''',
      'android/media/AudioManager.java':'''package android.media;public class AudioManager{public static final int STREAM_MUSIC=3,AUDIOFOCUS_GAIN_TRANSIENT=2,AUDIOFOCUS_REQUEST_GRANTED=1,GET_DEVICES_OUTPUTS=2;public interface OnAudioFocusChangeListener{void onAudioFocusChange(int c);}public int requestAudioFocus(OnAudioFocusChangeListener f,int s,int g){return 1;}public void abandonAudioFocus(OnAudioFocusChangeListener f){}public AudioDeviceInfo[] getDevices(int flags){return new AudioDeviceInfo[]{new AudioDeviceInfo()};}public boolean isStreamMute(int s){return false;}public int getStreamVolume(int s){return 6;}}''',
      'android/os/SystemClock.java':'''package android.os;public class SystemClock{public static long elapsedRealtime(){return System.nanoTime()/1000000;}}''',
      'android/util/Log.java':'''package android.util;public class Log{public static int i(String t,String s){return 0;}public static int w(String t,String s){System.err.println(s);return 0;}}''',
      'android/media/AudioTrack.java':'''package android.media;import java.io.*;public class AudioTrack{public static final int MODE_STREAM=1,WRITE_BLOCKING=0,STATE_INITIALIZED=1;public static AudioTrack last;public static volatile boolean stall,entered;public final ByteArrayOutputStream bytes=new ByteArrayOutputStream();public boolean playing,released,primed;public int releases;public AudioTrack(int a,int b,int c,int d,int e,int f){last=this;}public static int getMinBufferSize(int a,int b,int c){return 24000;}public int getState(){return 1;}public boolean setPreferredDevice(AudioDeviceInfo d){return true;}public AudioDeviceInfo getRoutedDevice(){return new AudioDeviceInfo();}public void play(){primed=bytes.size()>=24000;playing=true;}public int write(byte[] b,int at,int n){if(stall&&playing){entered=true;while(!released){try{Thread.sleep(1);}catch(Exception ignored){}}}if(released)throw new IllegalStateException("released");bytes.write(b,at,n);return n;}public int write(byte[] b,int at,int n,int mode){return write(b,at,n);}public int getPlaybackHeadPosition(){return bytes.size()/2;}public void pause(){}public void flush(){}public void stop(){}public void release(){released=true;releases++;}}''',
      'org/lira/receiver/DictationEngine.java':'''package org.lira.receiver;public class DictationEngine{public interface Completion{void done(boolean success);}public interface Audio{void stop();void play(String t,String h,String a,int n,Completion done);}}''',
      'org/lira/receiver/ReceiverService.java':'''package org.lira.receiver;public class ReceiverService{public static ReceiverService dictation;public void pause(String why){}}''',
      'org/lira/receiver/TaskAssetStore.java':'''package org.lira.receiver;public class TaskAssetStore{private final java.io.File file;public TaskAssetStore(java.io.File f){file=f;}public java.io.File read(String t,String h,String a){return file;}}''',
      'org/lira/receiver/ShortWordLifecycle.java':'''package org.lira.receiver;import java.io.*;import java.util.concurrent.*;import android.media.AudioTrack;public class ShortWordLifecycle{public static void main(String[] args)throws Exception{boolean cancel=args[1].equals("cancel");AudioTrack.stall=cancel;File word=new File(args[0]);try(FileOutputStream out=new FileOutputStream(word)){for(int n=0;n<9000;n++){out.write((n%100)+1);out.write(0);}}CountDownLatch done=new CountDownLatch(1);final boolean[] success={false};TaskAudioPlayer player=new TaskAudioPlayer(new android.content.Context(),new TaskAssetStore(word));player.play("t","h","a",1,new DictationEngine.Completion(){public void done(boolean ok){success[0]=ok;done.countDown();}});if(cancel){long until=System.currentTimeMillis()+2000;while(!AudioTrack.entered&&System.currentTimeMillis()<until)Thread.sleep(1);if(!AudioTrack.entered)throw new AssertionError("no active write");player.stop();if(done.await(100,TimeUnit.MILLISECONDS))throw new AssertionError("cancelled generation completed");if(AudioTrack.last.releases!=1)throw new AssertionError("cancel must release exactly once");System.out.println("cancel suppresses completion and unblocks active writer");return;}if(!done.await(5,TimeUnit.SECONDS)||!success[0])throw new AssertionError("playback callback");AudioTrack t=AudioTrack.last;byte[] submitted=t.bytes.toByteArray();int audible=0;for(int f=2400;f<submitted.length/2-6000;f++)if(submitted[f*2]!=0||submitted[f*2+1]!=0)audible++;if(audible!=9000)throw new AssertionError("short word clipped by startup/downstream tail: "+audible+" of 9000 source frames audible");if(!t.primed)throw new AssertionError("started an empty stream");if(t.releases!=1)throw new AssertionError("track release count");System.out.println("complete short-word payload survives delayed speaker startup and tail");}}'''
    }
    for rel,data in sources.items():
        p=tmp_path/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(data,encoding='utf-8')
    for name in ['TaskAudioPlayer.java','PcmDrainProof.java']:
        shutil.copyfile(ROOT/'src'/name,tmp_path/'org/lira/receiver'/name)
    classes=tmp_path/'classes';classes.mkdir()
    result=subprocess.run([javac,'-encoding','UTF-8','-d',str(classes),*[str(p) for p in tmp_path.rglob('*.java')]],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    result=subprocess.run([java,'-cp',str(classes),'org.lira.receiver.ShortWordLifecycle',str(tmp_path/'word.pcm'),mode],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stdout+result.stderr
