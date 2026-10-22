package org.lira.receiver;
import org.json.*;
/** Native remote mapping, shared with JVM tests. Key numbers are Android DPAD constants. */
public final class DictationRemote {
 public static String action(JSONObject s,int key,int repeats)throws Exception{
  String state=s.getString("state");if(state.equals("completed")||state.equals("cancelled"))return null;
  boolean recognition=s.getJSONArray("steps").getJSONObject(s.getInt("index")).getString("kind").equals("recognition"),manual=s.getJSONObject("task").getJSONObject("playback").optBoolean("manual_navigation");
  if(key==4)return repeats>0?"cancel":"pause";
  if(recognition){if(key==22)return state.equals("waiting_start")?"start":state.equals("paused")?"resume":"confirm";if(key==21&&state.equals("waiting_child"))return "previous";return null;}
  if(key==23||key==66)return state.equals("waiting_start")?"start":state.equals("paused")?"resume":manual?"replay":"confirm";
  if(key==21)return manual?"previous":"pause";if(key==22)return manual?"confirm":"replay";return null;
 }
}
