package org.lira.receiver;

import android.content.Context;
import android.media.*;
import android.os.SystemClock;
import android.util.Log;
import java.io.*;

/** Cached PCM with primed startup and a downstream speaker-tail guard. */
public final class TaskAudioPlayer implements DictationEngine.Audio {
    private static final int RATE=24000, SILENCE_BYTES=RATE; // 500 ms, mono s16
    private final Context context;
    private final TaskAssetStore assets;
    private volatile long generation;
    private volatile AudioTrack track;

    public TaskAudioPlayer(Context context,TaskAssetStore assets){this.context=context;this.assets=assets;}

    public synchronized void stop(){
        generation++;
        AudioTrack previous=track;track=null;
        if(previous!=null)try{previous.pause();previous.flush();previous.release();}catch(Exception ignored){}
    }

    private int write(AudioTrack output,byte[] data,int count,long token)throws IOException {
        int offset=0;
        while(offset<count){
            if(token!=generation)throw new IOException("playback cancelled");
            int written=output.write(data,offset,count-offset,AudioTrack.WRITE_BLOCKING);
            if(written<=0)throw new IOException("PCM write failed: "+written);
            offset+=written;
        }
        return count/2;
    }

    public void play(final String task,final String hash,final String asset,final int repeats,final DictationEngine.Completion done){
        stop();final long token=generation;
        new Thread(new Runnable(){public void run(){
            boolean success=false,acquired=false,bound=false;
            AudioTrack output=null;
            AudioManager manager=(AudioManager)context.getSystemService(Context.AUDIO_SERVICE);
            AudioManager.OnAudioFocusChangeListener focus=new AudioManager.OnAudioFocusChangeListener(){
                public void onAudioFocusChange(int change){
                    if(change<0&&token==generation){
                        stop();
                        try{if(ReceiverService.dictation!=null)ReceiverService.dictation.pause("focus_loss");}catch(Exception ignored){}
                    }
                }
            };
            try{
                if(repeats<1||repeats>10)throw new IOException("invalid repeats");
                if(manager.isStreamMute(AudioManager.STREAM_MUSIC)||manager.getStreamVolume(AudioManager.STREAM_MUSIC)==0)throw new IOException("speaker muted");
                acquired=manager.requestAudioFocus(focus,AudioManager.STREAM_MUSIC,AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)==AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
                if(!acquired)throw new IOException("focus denied");
                File file=assets.read(task,hash,asset);
                int minimum=AudioTrack.getMinBufferSize(RATE,AudioFormat.CHANNEL_OUT_MONO,AudioFormat.ENCODING_PCM_16BIT);
                if(minimum<=0||minimum>RATE*4)throw new IOException("unsupported output buffer");
                int capacity=Math.max(SILENCE_BYTES,minimum);capacity+=(capacity%2);
                output=new AudioTrack(AudioManager.STREAM_MUSIC,RATE,AudioFormat.CHANNEL_OUT_MONO,AudioFormat.ENCODING_PCM_16BIT,capacity,AudioTrack.MODE_STREAM);
                if(output.getState()!=AudioTrack.STATE_INITIALIZED)throw new IOException("AudioTrack unavailable");
                AudioDeviceInfo speaker=null;
                for(AudioDeviceInfo device:manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS))if(device.getType()==AudioDeviceInfo.TYPE_BUILTIN_SPEAKER){speaker=device;break;}
                if(speaker==null||!output.setPreferredDevice(speaker))throw new IOException("speaker unavailable");
                synchronized(TaskAudioPlayer.this){if(token!=generation)return;track=output;bound=true;}
                // Fill one complete buffer BEFORE play(). Startup cannot consume
                // the first syllable while the route/DSP wakes from standby.
                long frames=write(output,new byte[capacity],capacity,token),sourceFrames=0;
                output.play();
                byte[] block=new byte[8192];
                for(int repeat=0;repeat<repeats;repeat++)try(FileInputStream input=new FileInputStream(file)){
                    int count;
                    while((count=input.read(block))!=-1){
                        if((count&1)!=0)throw new IOException("partial PCM frame");
                        int written=write(output,block,count,token);frames+=written;sourceFrames+=written;
                    }
                }
                // Playback-head completion is a mixer receipt. Keep the route
                // alive for the downstream tail instead of stop/release on the
                // last spoken frame. Padding is not counted as a spoken repeat.
                frames+=write(output,new byte[SILENCE_BYTES],SILENCE_BYTES,token);
                long deadline=SystemClock.elapsedRealtime()+Math.max(5000,frames*1000/RATE+3000);
                long played;
                while((played=output.getPlaybackHeadPosition()&0xffffffffL)<frames){
                    if(token!=generation)throw new IOException("playback cancelled");
                    if(SystemClock.elapsedRealtime()>deadline)throw new IOException("drain timeout");
                    Thread.sleep(10);
                }
                AudioDeviceInfo route=output.getRoutedDevice();
                success=PcmDrainProof.completed(frames,played,route==null?-1:route.getType(),manager.isStreamMute(AudioManager.STREAM_MUSIC),manager.getStreamVolume(AudioManager.STREAM_MUSIC),token==generation);
                Log.i("LiraTaskAudio","source_frames="+sourceFrames+" guarded_frames="+frames+" played="+played+" route="+(route==null?-1:route.getType())+" volume="+manager.getStreamVolume(AudioManager.STREAM_MUSIC)+" success="+success);
            }catch(Exception error){Log.w("LiraTaskAudio","playback failed: "+error.getClass().getSimpleName()+": "+error.getMessage());}
            finally{
                if(output!=null)synchronized(TaskAudioPlayer.this){
                    // stop() already releases a bound cancelled track. A track
                    // failing before binding still belongs to this worker.
                    if(!bound||track==output){if(track==output)track=null;try{output.stop();output.release();}catch(Exception ignored){}}
                }
                if(acquired)manager.abandonAudioFocus(focus);
                if(token==generation)done.done(success);
            }
        }},"LiraTaskAudio").start();
    }
}
