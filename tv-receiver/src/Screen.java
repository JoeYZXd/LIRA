package org.lira.receiver;
import android.app.Activity;
import android.content.*;
import android.graphics.Color;
import android.os.Bundle;
import android.view.*;
import android.widget.*;
import org.json.*;
import java.util.UUID;

public class Screen extends Activity {
    static volatile Screen active;
    private final DictationKeyGate dictationKeys=new DictationKeyGate();
    private final BroadcastReceiver sleep = new BroadcastReceiver() {
        public void onReceive(Context c, Intent i) { finish(); }
    };
    @Override public void onCreate(Bundle b) {
        super.onCreate(b);
        registerReceiver(sleep, new IntentFilter(Intent.ACTION_SCREEN_OFF));
        startService(new Intent(this, ReceiverService.class));
        render();
    }
    @Override public void onNewIntent(Intent i) { super.onNewIntent(i); setIntent(i); render(); }
    @Override public void onResume() { super.onResume(); active=this;
        try{if(ReceiverService.dictation!=null&&ReceiverService.dictation.active()){JSONObject state=ReceiverService.dictation.status();if(state.optInt("schema_version")==2){ReceiverService.dictation.displaySize(getResources().getDisplayMetrics().widthPixels,getResources().getDisplayMetrics().heightPixels);ReceiverService.dictation.renderCurrent();return;}JSONObject step=state.getJSONArray("steps").getJSONObject(state.getInt("index"));String kind=step.getString("kind");renderTask(kind,kind.equals("recognition")?step.getJSONArray("words"):new JSONArray(),state.getString("state").startsWith("paused")?"已暂停，按继续恢复":"默写进行中");}}catch(Exception ignored){} }
    @Override public void onPause() {
        try{if(ReceiverService.dictation!=null)ReceiverService.dictation.pause("foreground_exit");}catch(Exception ignored){}
        if (active==this) {active=null;if(ReceiverService.audio!=null)ReceiverService.audio.stop();}
        super.onPause();
    }
    @Override public void onDestroy() { unregisterReceiver(sleep); super.onDestroy(); }
    private boolean manualDictation(){try{JSONObject s=ReceiverService.dictation.status();return s.getJSONObject("task").getJSONObject("playback").optBoolean("manual_navigation")&&s.getJSONArray("steps").getJSONObject(s.getInt("index")).getString("kind").equals("dictation");}catch(Exception ignored){return false;}}
    void renderTask(String kind,JSONArray words,String text) {
        if(kind.startsWith("v2_")){try{setContentView(new LearningTaskView(this,ReceiverService.dictation.status(),words));return;}catch(Exception ignored){}}
        LinearLayout root=new LinearLayout(this);root.setOrientation(LinearLayout.VERTICAL);root.setPadding(40,24,40,24);root.setBackgroundColor(Color.rgb(15,27,32));root.addView(label("LIRA小助手",20,Color.WHITE));
        try{if(words.length()>0){root.addView(new WordPageView(this,words),new LinearLayout.LayoutParams(-1,0,1));if(kind.startsWith("v2_"))root.addView(label(text,16,Color.LTGRAY));}else {TextView t=label(text,kind.startsWith("v2_")?24:44,Color.WHITE);t.setGravity(Gravity.CENTER);root.addView(t,new LinearLayout.LayoutParams(-1,0,1));}}catch(Exception ignored){}
        root.addView(label(kind.startsWith("v2_")?(manualDictation()?"左：上一词并播放 · 右：下一词/末项结束 · 确认：重播 · 返回：暂停/长按结束":"确认：开始/下一项/恢复 · 左键：暂停 · 右键：重播 · 长按返回：结束"):"返回上一项并暂停 · 确认继续 · 右键下一项",18,Color.LTGRAY));setContentView(root);
    }
    @Override public boolean onKeyDown(int key,KeyEvent event){
        if(ReceiverService.dictation!=null&&ReceiverService.dictation.active()){
            try{JSONObject s=ReceiverService.dictation.status();if(s.optInt("schema_version")==2){String action=DictationRemote.action(s,key,event.getRepeatCount());if(action==null){if(key==KeyEvent.KEYCODE_DPAD_CENTER||key==KeyEvent.KEYCODE_ENTER||key==KeyEvent.KEYCODE_DPAD_UP||key==KeyEvent.KEYCODE_DPAD_DOWN||key==KeyEvent.KEYCODE_DPAD_LEFT||key==KeyEvent.KEYCODE_DPAD_RIGHT||key==KeyEvent.KEYCODE_MENU)return true;return super.onKeyDown(key,event);}if(key!=KeyEvent.KEYCODE_BACK&&!dictationKeys.accept(true,event.getRepeatCount(),android.os.SystemClock.elapsedRealtime()))return true;ReceiverService.dictation.localControl(action);return true;}}catch(Exception ignored){return true;}
            String command=key==KeyEvent.KEYCODE_BACK?"previous":key==KeyEvent.KEYCODE_DPAD_CENTER||key==KeyEvent.KEYCODE_ENTER?"continue":key==KeyEvent.KEYCODE_DPAD_RIGHT?"next":null;
            if(command!=null){try{JSONObject s=ReceiverService.dictation.status();boolean recitation=command.equals("continue")&&s.getString("state").equals("running")&&s.getJSONArray("steps").getJSONObject(s.getInt("index")).getString("kind").equals("recitation")&&s.getJSONObject("task").getJSONObject("parameters").getString("recitation_end_mode").equals("remote_confirm");ReceiverService.dictation.control(new JSONObject().put("task_id",s.getJSONObject("task").getString("task_id")).put("expected_state_revision",s.getLong("state_revision")).put("command_id",UUID.randomUUID().toString()).put("command",recitation?"recitation_complete":command).put("source","remote_confirm"));}catch(Exception ignored){}return true;}
        }return super.onKeyDown(key,event);
    }
    private TextView label(String text, int size, int color) {
        TextView v=new TextView(this);v.setText(text);v.setTextSize(size);v.setTextColor(color);return v;
    }
    void clearContent(Runnable acknowledged) {
        render();
        getWindow().getDecorView().post(acknowledged);
    }
    private void render() {
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_FULLSCREEN);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        LinearLayout root=new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(40,24,40,24);root.setBackgroundColor(Color.rgb(15,27,32));
        root.addView(label("LIRA小助手",18,Color.rgb(131,212,189)));
        ReceiverService.Content content=ReceiverService.current;
        if(content==null) {
            TextView ready=label("准备好，听你说",44,Color.WHITE);
            ready.setGravity(Gravity.CENTER);
            root.addView(ready,new LinearLayout.LayoutParams(-1,0,1));
        } else if(content.bitmap!=null) {
            ImageView picture=new ImageView(this);picture.setImageBitmap(content.bitmap);
            picture.setScaleType(ImageView.ScaleType.FIT_CENTER);picture.setContentDescription(content.text);
            root.addView(picture,new LinearLayout.LayoutParams(-1,0,1));
            if(!content.text.isEmpty())root.addView(label(content.text,22,Color.WHITE));
        } else {
            ScrollView scroll=new ScrollView(this);scroll.setFillViewport(true);
            TextView body=label(content.text,content.text.length()>100?30:48,Color.WHITE);
            body.setGravity(Gravity.CENTER);body.setPadding(20,20,20,20);body.setLineSpacing(8,1.15f);
            scroll.addView(body);root.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));
            scroll.setFocusable(true);scroll.requestFocus();
        }
        root.addView(label("返回退出展示  ·  长文可用上下键滚动",14,Color.rgb(165,184,188)));
        setContentView(root);
        if(content!=null) root.post(new Runnable(){public void run() {
            if(ReceiverService.current==content)ReceiverService.renderedId=content.id;
        }});
    }
}
