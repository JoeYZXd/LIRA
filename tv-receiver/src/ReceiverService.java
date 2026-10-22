package org.lira.receiver;
import android.app.*;
import android.content.*;
import android.graphics.*;
import android.os.*;
import android.util.Base64;
import org.json.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;

/** Local-only bounded HTTP endpoint. Never executes commands, fetches URLs or wakes the TV. */
public class ReceiverService extends Service {
    static class Content {
        final String id,text; final Bitmap bitmap;
        Content(String id,String text,Bitmap bitmap){this.id=id;this.text=text;this.bitmap=bitmap;}
    }
    static volatile Content current;
    static volatile String renderedId="";
    private final Set<String> clearedIds=new LinkedHashSet<>();
    private volatile boolean running;
    static volatile AudioReceiver audio;
    static volatile DictationEngine dictation;
    private TaskAssetStore taskAssets;
    private ServerSocket server;
    private final Handler main=new Handler(Looper.getMainLooper());
    private final String instance=UUID.randomUUID().toString();
    private static final int LIMIT=4*1024*1024;
    /** Bound the whole request, not just pauses between attacker-controlled bytes. */
    private static final class TimedInput extends FilterInputStream {
        private final Socket socket;
        private final long deadline=SystemClock.elapsedRealtime()+4000;
        TimedInput(Socket socket)throws IOException {super(socket.getInputStream());this.socket=socket;}
        private void limit()throws IOException {
            long remaining=deadline-SystemClock.elapsedRealtime();
            if(remaining<=0)throw new SocketTimeoutException("request deadline");
            socket.setSoTimeout((int)remaining);
        }
        @Override public int read()throws IOException {limit();return in.read();}
        @Override public int read(byte[] b,int off,int len)throws IOException {limit();return in.read(b,off,len);}
    }
    @Override public void onCreate() {
        super.onCreate();running=true;audio=new AudioReceiver(this);
        try {
            taskAssets=new TaskAssetStore(new File(getFilesDir(),"task-assets"));
            dictation=new DictationEngine(new DictationEngine.Clock(){public long now(){return SystemClock.elapsedRealtime();}},new TaskAudioPlayer(this,taskAssets),new DictationEngine.Display(){public void show(final String kind,final JSONArray words,final String text){main.post(new Runnable(){public void run(){if(Screen.active!=null)Screen.active.renderTask(kind,words,text);}});}},new DictationStateStore(new File(getFilesDir(),"dictation-state.json")),taskAssets);
            dictation.setExplanationAudio(audio);dictation.setDebug(getSharedPreferences("dictation-display",0).getBoolean("debug",false));
            main.post(new Runnable(){public void run(){try{dictation.tick();}catch(Exception ignored){}if(running)main.postDelayed(this,100);}});
        }catch(Exception e){dictation=null;}

        Notification n=new Notification.Builder(this).setContentTitle("LIRA小助手")
            .setContentText("已准备接收展示内容").setSmallIcon(android.R.drawable.ic_menu_view).build();
        startForeground(1,n);
        new Thread(new Runnable(){public void run(){listen();}},"LiraReceiver").start();
    }
    @Override public int onStartCommand(Intent i,int flags,int id){return START_STICKY;}
    @Override public IBinder onBind(Intent i){return null;}
    @Override public void onDestroy(){running=false;if(audio!=null)audio.stop();try{if(dictation!=null)dictation.pause("service_stop");}catch(Exception ignored){}try{if(server!=null)server.close();}catch(IOException ignored){}super.onDestroy();}
    private void listen() {
        while(running) {
            try {
                server=new ServerSocket();server.setReuseAddress(true);server.bind(new InetSocketAddress(40100));
                while(running)try(Socket socket=server.accept()) {
                    socket.setSoTimeout(4000);
                    handle(socket);
                }catch(Exception ignored){}
            }catch(IOException ignored){}
            finally{try{if(server!=null)server.close();}catch(IOException ignored){}}
            if(running)try{Thread.sleep(2000);}catch(InterruptedException ignored){}
        }
    }
    private static String line(InputStream in) throws IOException {
        ByteArrayOutputStream b=new ByteArrayOutputStream();
        for(int x;(x=in.read())!=-1;) {
            if(x==10)return new String(b.toByteArray(),StandardCharsets.US_ASCII).trim();
            if(b.size()>=4096)throw new IOException("header too long");b.write(x);
        }
        throw new EOFException();
    }
    private void reply(Socket s,int code,JSONObject data) throws IOException {
        byte[] body=data.toString().getBytes(StandardCharsets.UTF_8);
        OutputStream out=s.getOutputStream();
        out.write(("HTTP/1.1 "+code+" Result\r\nContent-Type: application/json; charset=utf-8\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: "+body.length+"\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
        out.write(body);out.flush();
    }
    private void error(Socket s,int code,String message)throws Exception{reply(s,code,new JSONObject().put("error",message));}
    private boolean awake(){return ((PowerManager)getSystemService(POWER_SERVICE)).isInteractive();}
    private void handle(Socket socket) throws Exception {
        InputStream in=new BufferedInputStream(new TimedInput(socket));String[] request=line(in).split(" ");
        Map<String,String> headers=new HashMap<>();int total=0;
        for(String h;!(h=line(in)).isEmpty();) {
            total+=h.length();if(total>16384)throw new IOException("headers too large");
            int colon=h.indexOf(':');if(colon<1)throw new IOException("bad header");
            String key=h.substring(0,colon).toLowerCase(Locale.ROOT);
            if(headers.put(key,h.substring(colon+1).trim())!=null)throw new IOException("duplicate header");
        }
        String supplied=headers.get("authorization");
        if(supplied==null||!MessageDigest.isEqual(supplied.getBytes(StandardCharsets.UTF_8),("Bearer "+Pairing.TOKEN).getBytes(StandardCharsets.UTF_8))) {
            error(socket,401,"unauthorized");return;
        }
        if(request.length!=3||headers.containsKey("origin")||headers.containsKey("transfer-encoding")) {error(socket,400,"unsupported request");return;}
        if(request[0].equals("GET")&&request[1].equals("/status")) {
            reply(socket,200,new JSONObject().put("interactive",awake()).put("foreground",Screen.active!=null)
                .put("instance",instance).put("rendered_id",renderedId).put("version","0.4").put("audio",audio.status())
                .put("capabilities",new JSONArray().put("clear_content_id").put("pcm_audio_v1").put(dictation==null?"dictation_unavailable":DictationSpec.CAPABILITY).put(dictation==null?"dictation_unavailable":"dictation_task_v2").put(dictation==null?"dictation_unavailable":"dictation_recognition_words_v1").put(dictation==null?"dictation_unavailable":"dictation_manual_navigation_v1").put("dictation_display_settings_v1").put("dictation_recognition_navigation_v1")));return;
        }
        if(request[0].equals("GET")&&request[1].equals("/dictation/status")){if(dictation==null){error(socket,503,"dictation unavailable");return;}reply(socket,200,dictation.status());return;}
        if(!request[0].equals("POST")||!(request[1].equals("/show")||request[1].equals("/clear")||request[1].matches("/audio/(start|chunk|end|stop)")||request[1].matches("/dictation/(start|control|events|ack|lease|display|assets/(start|chunk|commit))"))){error(socket,404,"not found");return;}
        int length;
        try{length=Integer.parseInt(headers.get("content-length"));}catch(Exception e){error(socket,411,"length required");return;}
        if(length<=0||length>LIMIT){error(socket,413,"payload too large");return;}
        byte[] body=new byte[length];new DataInputStream(in).readFully(body);
        JSONObject data;
        try{data=new JSONObject(new String(body,StandardCharsets.UTF_8));}catch(JSONException e){error(socket,400,"invalid JSON");return;}
        if(request[1].startsWith("/dictation/")) {
            try {
                if(dictation==null)throw new IllegalArgumentException("dictation unavailable");
                String path=request[1];JSONObject result;
                if(path.equals("/dictation/display")){if(data.length()!=1||!data.has("debug_enabled")||!(data.get("debug_enabled") instanceof Boolean))throw new IllegalArgumentException("invalid_display_settings");boolean value=data.getBoolean("debug_enabled");if(!getSharedPreferences("dictation-display",0).edit().putBoolean("debug",value).commit())throw new IllegalStateException("display_settings_not_saved");dictation.setDebug(value);result=new JSONObject().put("debug_enabled",value);}
                else if(path.equals("/dictation/start")){if(!awake()||Screen.active==null)throw new IllegalArgumentException("interactive foreground required");if(audio.status().optString("status").matches("buffering|playing|draining"))throw new IllegalArgumentException("legacy audio busy");dictation.displaySize(getResources().getDisplayMetrics().widthPixels,getResources().getDisplayMetrics().heightPixels);result=dictation.start(data);}
                else if(path.equals("/dictation/control"))result=dictation.control(data);
                else if(path.matches("/dictation/(events|ack|lease)")){
                    String method=path.substring(path.lastIndexOf('/')+1);
                    result=method.equals("events")?dictation.events(data):method.equals("ack")?dictation.ack(data):dictation.lease(data);
                }
                else {String task=data.getString("task_id"),hash=data.getString("content_hash"),asset=data.getString("asset_id");boolean ready=false;
                    if(path.endsWith("/start"))ready=taskAssets.begin(task,hash,asset,data.getLong("pcm_bytes"),data.getString("sha256"));
                    else if(path.endsWith("/chunk"))taskAssets.append(task,hash,asset,data.getLong("offset"),Base64.decode(data.getString("pcm_base64"),Base64.NO_WRAP));
                    else {taskAssets.commit(task,hash,asset);ready=true;}
                    result=new JSONObject().put("status","ok").put("ok",true).put("ready",ready);
                }reply(socket,200,result);
            }catch(Exception e){error(socket,409,"dictation_rejected");}return;
        }
        if(request[1].startsWith("/audio/")) {
            try{reply(socket,200,dictation==null?audio.request(request[1],data):dictation.explanationAudio(request[1],data));}
            catch(Exception e){error(socket,409,e.getMessage());}
            return;
        }
        if(dictation!=null&&dictation.active()){error(socket,409,"dictation owns display");return;}
        if(request[1].equals("/clear")) {
            final String expected=data.optString("expected_content_id","");
            if(!expected.matches("[a-zA-Z0-9-]{1,64}")){error(socket,400,"invalid content ID");return;}
            final CountDownLatch done=new CountDownLatch(1);
            final String[] result={"unavailable"};
            final long expires=SystemClock.elapsedRealtime()+2000;
            main.post(new Runnable(){public void run(){
                if(SystemClock.elapsedRealtime()>expires||(dictation!=null&&dictation.active())){done.countDown();return;}
                if(clearedIds.contains(expected)){result[0]="cleared";done.countDown();return;}
                Screen screen=Screen.active;
                if(!awake()||screen==null){done.countDown();return;}
                if(current==null||!current.id.equals(expected)||!renderedId.equals(expected)) {
                    result[0]="content mismatch";done.countDown();return;
                }
                current=null;renderedId="";
                screen.clearContent(new Runnable(){public void run(){
                    clearedIds.add(expected);
                    if(clearedIds.size()>64)clearedIds.remove(clearedIds.iterator().next());
                    result[0]="cleared";done.countDown();
                }});
            }});
            if(done.await(2500,TimeUnit.MILLISECONDS)&&result[0].equals("cleared"))
                reply(socket,200,new JSONObject().put("cleared_id",expected));
            else error(socket,409,result[0]);
            return;
        }
        if(!data.optBoolean("approved",false)){error(socket,403,"foreground consent required");return;}
        if(!awake()){error(socket,409,"screen not interactive");return;}
        String id=data.optString("id",""),text=data.optString("text",""),type=data.optString("type","");
        if(!id.matches("[a-zA-Z0-9-]{1,64}")||text.length()>8000){error(socket,400,"invalid content");return;}
        if(current!=null&&current.id.equals(id)){reply(socket,200,new JSONObject().put("accepted_id",id));return;}
        Bitmap bitmap=null;
        if(type.equals("image")) {
            try {
                byte[] image=Base64.decode(data.getString("image_base64"),Base64.DEFAULT);
                BitmapFactory.Options opts=new BitmapFactory.Options();opts.inJustDecodeBounds=true;
                BitmapFactory.decodeByteArray(image,0,image.length,opts);
                if(opts.outWidth<1||opts.outHeight<1||opts.outWidth>8192||opts.outHeight>8192)throw new IllegalArgumentException();
                opts.inSampleSize=1;
                while(opts.outWidth/opts.inSampleSize>1920||opts.outHeight/opts.inSampleSize>1080)opts.inSampleSize*=2;
                opts.inJustDecodeBounds=false;bitmap=BitmapFactory.decodeByteArray(image,0,image.length,opts);
                if(bitmap==null)throw new IllegalArgumentException();
            }catch(Exception e){error(socket,400,"invalid image");return;}
            if(text.length()>160){error(socket,400,"caption too long");return;}
        }else if(!type.equals("text")||text.trim().isEmpty()){error(socket,400,"invalid text");return;}
        Content content=new Content(id,text,bitmap);
        main.post(new Runnable(){public void run() {
            if(!awake()||(dictation!=null&&dictation.active()))return;
            current=content;renderedId="";
            startActivity(new Intent(ReceiverService.this,Screen.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK|Intent.FLAG_ACTIVITY_SINGLE_TOP));
        }});
        reply(socket,202,new JSONObject().put("accepted_id",id));
    }
}
