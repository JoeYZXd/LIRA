package org.lira.receiver;

import android.content.Context;
import android.media.*;
import android.os.*;
import android.util.Base64;
import org.json.*;
import java.util.*;
import java.util.concurrent.*;

/** In-memory mono PCM transport. One owned stream, bounded queue, no URL fetching. */
final class AudioReceiver implements DictationEngine.ExplanationAudio {
    private final Context context;
    private final Set<String> used=new LinkedHashSet<>();
    private volatile Stream current;
    private static final byte[] END=new byte[0];
    private static final class Stream {
        final String id;
        final ArrayBlockingQueue<byte[]> queue=new ArrayBlockingQueue<>(4);
        volatile String state="buffering",error="";
        volatile boolean cancelled,ended,finished;
        volatile long written,played,received;
        volatile int sessionId,routeType;
        int sequence;
        volatile AudioTrack track;
        Stream(String id){this.id=id;}
        boolean active(){return state.equals("buffering")||state.equals("playing")||state.equals("draining");}
    }
    // Keep ownership through cancellation/failure cleanup, not merely until state changes.
    public boolean busy(){Stream s=current;return s!=null&&!s.finished;}
    AudioReceiver(Context context){this.context=context;}
    private boolean visible(){return ((PowerManager)context.getSystemService(Context.POWER_SERVICE)).isInteractive()&&Screen.active!=null;}
    synchronized JSONObject status() throws JSONException {
        Stream s=current;
        AudioManager manager=(AudioManager)context.getSystemService(Context.AUDIO_SERVICE);
        JSONObject value=new JSONObject().put("status",s==null?"idle":s.state)
            .put("music_volume",manager.getStreamVolume(AudioManager.STREAM_MUSIC))
            .put("music_muted",manager.isStreamMute(AudioManager.STREAM_MUSIC));
        if(s!=null)value.put("id",s.id).put("written_frames",s.written).put("played_frames",s.played)
            .put("audio_session_id",s.sessionId).put("routed_device_type",s.routeType)
            .put("received_bytes",s.received).put("next_sequence",s.sequence).put("error",s.error);
        return value;
    }
    public synchronized JSONObject request(String path,JSONObject data) throws Exception {
        String id=data.optString("id","");
        if(!id.matches("[a-zA-Z0-9-]{1,64}"))throw new IllegalArgumentException("invalid audio ID");
        Stream s=current;
        if(path.equals("/audio/stop")) {
            if(s!=null&&s.id.equals(id))stop();
            return new JSONObject().put("stopped_id",id);
        }
        if(!visible())throw new IllegalArgumentException("audio requires interactive foreground receiver");
        if(path.equals("/audio/start")) {
            if(!data.optBoolean("approved",false)||data.optInt("sample_rate")!=24000
                    ||data.optInt("channels")!=1||!data.optString("format").equals("s16le"))
                throw new IllegalArgumentException("audio consent or format invalid");
            if(s!=null&&s.id.equals(id))return status(); // Retry never restarts playback.
            if(used.contains(id))throw new IllegalArgumentException("retired audio ID");
            if(s!=null&&!s.finished)throw new IllegalArgumentException("audio busy");
            final Stream next=new Stream(id);current=next;used.add(id);
            if(used.size()>64)used.remove(used.iterator().next());
            new Thread(new Runnable(){public void run(){play(next);}},"LiraAudio").start();
            return status();
        }
        if(s==null||!s.id.equals(id)||!s.active()||s.ended)throw new IllegalArgumentException("audio ownership or state mismatch");
        if(path.equals("/audio/chunk")) {
            int sequence=data.getInt("sequence");
            if(sequence!=s.sequence)throw new IllegalArgumentException("audio sequence mismatch");
            String encoded=data.getString("pcm_base64");
            if(encoded.length()>32000)throw new IllegalArgumentException("audio chunk too large");
            byte[] pcm=Base64.decode(encoded,Base64.NO_WRAP);
            if(pcm.length==0||pcm.length>24000||pcm.length%2!=0||s.received+pcm.length>5760000)
                throw new IllegalArgumentException("invalid PCM size");
            if(!s.queue.offer(pcm))throw new IllegalArgumentException("audio buffer full");
            s.received+=pcm.length;s.sequence++;
            return status();
        }
        if(path.equals("/audio/end")) {
            if(data.getLong("total_bytes")!=s.received||s.received==0)throw new IllegalArgumentException("audio size mismatch");
            if(!s.queue.offer(END))throw new IllegalArgumentException("audio buffer full");
            s.ended=true;return status();
        }
        throw new IllegalArgumentException("unknown audio endpoint");
    }
    synchronized void stop() { stop(current); }
    private synchronized void stop(Stream s) {
        if(current!=s)return;
        if(s==null||!s.active())return;
        s.cancelled=true;s.state="cancelled";s.queue.clear();s.queue.offer(END);
        AudioTrack t=s.track;
        if(t!=null)try{t.pause();t.flush();}catch(IllegalStateException ignored){}
    }
    private void play(Stream s) {
        AudioTrack track=null;
        AudioManager manager=(AudioManager)context.getSystemService(Context.AUDIO_SERVICE);
        AudioManager.OnAudioFocusChangeListener focus=new AudioManager.OnAudioFocusChangeListener(){
            public void onAudioFocusChange(int change){if(change<0)stop(s);}
        };
        boolean acquired=false;
        String terminal="completed";
        try {
            if(manager.isStreamMute(AudioManager.STREAM_MUSIC)||manager.getStreamVolume(AudioManager.STREAM_MUSIC)==0)
                throw new IllegalStateException("TV music output muted");
            acquired=manager.requestAudioFocus(focus,AudioManager.STREAM_MUSIC,AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)==AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
            if(!acquired)throw new IllegalStateException("audio focus denied");
            int capacity=Math.max(4800,AudioTrack.getMinBufferSize(24000,AudioFormat.CHANNEL_OUT_MONO,AudioFormat.ENCODING_PCM_16BIT));
            track=new AudioTrack(AudioManager.STREAM_MUSIC,24000,AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT,capacity,AudioTrack.MODE_STREAM);
            if(track.getState()!=AudioTrack.STATE_INITIALIZED)throw new IllegalStateException("AudioTrack unavailable");
            AudioDeviceInfo speaker=null;
            for(AudioDeviceInfo device:manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS))
                if(device.getType()==AudioDeviceInfo.TYPE_BUILTIN_SPEAKER){speaker=device;break;}
            if(speaker==null||!track.setPreferredDevice(speaker))throw new IllegalStateException("TV speaker route unavailable");
            s.sessionId=track.getAudioSessionId();
            synchronized(this){s.track=track;if(s.cancelled)return;track.play();s.state="playing";}
            while(!s.cancelled) {
                byte[] pcm=s.queue.poll(10,TimeUnit.SECONDS);
                if(pcm==null)throw new IllegalStateException("audio upload stalled");
                if(!visible())throw new IllegalStateException("receiver dismissed");
                if(pcm==END)break;
                int offset=0;
                while(offset<pcm.length&&!s.cancelled) {
                    int n=track.write(pcm,offset,pcm.length-offset,AudioTrack.WRITE_BLOCKING);
                    if(n<=0)throw new IllegalStateException("AudioTrack write failed");
                    offset+=n;s.written+=n/2;
                }
                AudioDeviceInfo route=track.getRoutedDevice();
                if(route!=null){s.routeType=route.getType();
                    if(s.routeType!=AudioDeviceInfo.TYPE_BUILTIN_SPEAKER)throw new IllegalStateException("TV speaker route not active");}
                s.played=((long)track.getPlaybackHeadPosition() & 0xffffffffL);
            }
            if(s.cancelled)return;
            if(s.routeType!=AudioDeviceInfo.TYPE_BUILTIN_SPEAKER)throw new IllegalStateException("TV speaker route unconfirmed");
            s.state="draining";
            long deadline=SystemClock.elapsedRealtime()+3000;
            while(!s.cancelled&&s.played<s.written) {
                if(!visible()||SystemClock.elapsedRealtime()>deadline)throw new IllegalStateException("audio drain unconfirmed");
                AudioDeviceInfo route=track.getRoutedDevice();
                if(route==null||route.getType()!=AudioDeviceInfo.TYPE_BUILTIN_SPEAKER)
                    throw new IllegalStateException("TV speaker route lost during drain");
                Thread.sleep(20);s.played=((long)track.getPlaybackHeadPosition() & 0xffffffffL);
            }

            if(!s.cancelled) {
                AudioDeviceInfo route=track.getRoutedDevice();
                if(route==null||route.getType()!=AudioDeviceInfo.TYPE_BUILTIN_SPEAKER)
                    throw new IllegalStateException("TV speaker route lost at completion");
                s.routeType=route.getType();
            }
        }catch(Exception e){s.error=e.getMessage()==null?"audio failed":e.getMessage();terminal="failed";}
        finally {
            synchronized(this){s.track=null;}
            if(track!=null){try{track.pause();track.flush();}catch(Exception ignored){}track.release();}
            if(acquired)manager.abandonAudioFocus(focus);
            synchronized(this){s.finished=true;s.state=s.cancelled?"cancelled":terminal;}
        }
    }
}
