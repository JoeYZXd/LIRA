package org.lira.receiver;
import org.json.*;
import java.util.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/** Pure bounded wire validation. This class never starts playback or grants authorization. */
public final class DictationSpec {
    public static final String CAPABILITY="dictation_task_v1";
    public static final String V2_CAPABILITY="dictation_task_v2";
    public final JSONObject task;
    private DictationSpec(JSONObject value) throws Exception { task=new JSONObject(value.toString()); }
    static void require(boolean ok,String path)throws Exception {if(!ok)throw new IllegalArgumentException(path);}
    static void keys(JSONObject o,String... keys)throws Exception {
        Set<String> expected=new HashSet<>(Arrays.asList(keys));
        require(o.length()==expected.size(),"missing or unknown fields");
        Iterator<String> i=o.keys();while(i.hasNext())require(expected.contains(i.next()),"unknown field");
    }
    static String text(Object value,int max,boolean id)throws Exception {
        require(value instanceof String,"expected text");String s=(String)value;
        require(s.length()>0&&s.codePointCount(0,s.length())<=max,"text length");
        for(int n=0;n<s.length();n++)require(s.charAt(n)>=32,"control character");
        if(id)require(s.matches("[A-Za-z0-9_.:-]+"),"identifier");return s;
    }
    static long integer(Object value,int min,int max)throws Exception {
        require(value instanceof Integer||value instanceof Long,"expected integer");long n=((Number)value).longValue();
        require(n>=min&&n<=max,"integer range");return n;
    }
    static void choice(JSONObject p,String key,String... allowed)throws Exception {
        require(Arrays.asList(allowed).contains(p.get(key)),"unsupported "+key);
    }
    public static void validateParameters(JSONObject p)throws Exception {
        keys(p,"advance_mode","expanded_word_seconds","expanded_repeat_count","expanded_timer_anchor","recognition_seconds_per_character","resume_timer_policy","recitation_end_mode");
        choice(p,"advance_mode","automatic","manual");
        integer(p.get("expanded_word_seconds"),1,600);integer(p.get("expanded_repeat_count"),1,5);
        integer(p.get("recognition_seconds_per_character"),1,600);
        choice(p,"expanded_timer_anchor","after_playback","from_item_start");
        choice(p,"resume_timer_policy","restart_current","remaining");
        choice(p,"recitation_end_mode","observed_user_signal","remote_confirm");
    }
    static Set<String> targets(JSONArray a)throws Exception {
        require(a.length()<=200,"too many targets");Set<String>s=new HashSet<>();
        for(int n=0;n<a.length();n++){String t=text(a.get(n),1,false);require(t.length()==1&&t.charAt(0)>=0x3400&&t.charAt(0)<=0x9fff,"Han target");require(s.add(t),"duplicate target");}return s;
    }
    static void words(JSONArray a,Set<String> targets,boolean audio)throws Exception {
        require(a.length()<=200,"too many words");Set<String> covered=new HashSet<>();
        for(int n=0;n<a.length();n++){
            JSONObject w=a.getJSONObject(n);
            if(audio)keys(w,"text","covers","audio_asset_id","pinyin_required_for");else keys(w,"text","covers");
            String word=text(w.get("text"),32,false);JSONArray c=w.getJSONArray("covers");Set<String> local=new HashSet<>();require(c.length()>0,"empty covers");
            for(int j=0;j<c.length();j++){String t=text(c.get(j),1,false);require(targets.contains(t)&&word.contains(t)&&local.add(t),"invalid covers");covered.add(t);}
            if(audio){text(w.get("audio_asset_id"),128,true);JSONArray r=w.getJSONArray("pinyin_required_for");Set<String> seen=new HashSet<>();for(int j=0;j<r.length();j++){String t=text(r.get(j),1,false);require(local.contains(t)&&seen.add(t),"pinyin requirement");}}
        }require(covered.equals(targets),"incomplete coverage");
    }
    public static DictationSpec parse(JSONObject value)throws Exception {
        if(value.optInt("schema_version",0)==2)return parseV2(value);
        keys(value,"schema_version","task_id","content_hash","config_revision","lessons","parameters","callback_registration_id");
        integer(value.get("schema_version"),1,1);integer(value.get("config_revision"),1,Integer.MAX_VALUE);
        text(value.get("task_id"),128,true);text(value.get("callback_registration_id"),128,true);
        validateParameters(value.getJSONObject("parameters"));JSONArray lessons=value.getJSONArray("lessons");
        require(lessons.length()>0&&lessons.length()<=50,"lessons length");Set<String> ids=new HashSet<>();
        for(int n=0;n<lessons.length();n++){
            JSONObject l=lessons.getJSONObject(n);keys(l,"lesson_id","source","four_targets","expanded_words","two_targets","recognition_words","recitation","focus_targets");
            require(ids.add(text(l.get("lesson_id"),128,true)),"duplicate lesson");JSONObject source=l.getJSONObject("source");keys(source,"book_id","unit","number","revision");
            text(source.get("book_id"),128,true);for(String k:new String[]{"unit","number","revision"})integer(source.get(k),1,Integer.MAX_VALUE);
            words(l.getJSONArray("expanded_words"),targets(l.getJSONArray("four_targets")),true);
            words(l.getJSONArray("recognition_words"),targets(l.getJSONArray("two_targets")),false);
            require(l.getJSONArray("expanded_words").length()+l.getJSONArray("recognition_words").length()>0,"empty lesson");
            Set<String> all=targets(l.getJSONArray("four_targets"));all.addAll(targets(l.getJSONArray("two_targets")));JSONArray focus=l.getJSONArray("focus_targets");Set<String> seenFocus=new HashSet<>();for(int j=0;j<focus.length();j++){String t=text(focus.get(j),1,false);require(all.contains(t)&&seenFocus.add(t),"focus target");}
            if(!l.isNull("recitation")){JSONObject r=l.getJSONObject("recitation");keys(r,"text","prompt_audio_asset_id");require(r.get("text") instanceof String,"recitation text");text(((String)r.get("text")).replace('\n',' ').replace('\r',' ').replace('\t',' '),16000,false);text(r.get("prompt_audio_asset_id"),128,true);}
        }
        require(value.toString().getBytes(StandardCharsets.UTF_8).length<=262144,"payload too large");
        String hash=text(value.get("content_hash"),64,false);require(hash.matches("[0-9a-f]{64}")&&hash.equals(contentHash(value)),"content_hash mismatch");
        return new DictationSpec(value);
    }
    public static void validateParametersV2(JSONObject p)throws Exception {
        if(p.has("manual_navigation")){keys(p,"repeat_count","interval_ms","speech_rate","manual_navigation");require(p.get("manual_navigation") instanceof Boolean,"manual_navigation boolean");require(!p.getBoolean("manual_navigation")||p.getInt("repeat_count")==1,"manual navigation plays once");}else keys(p,"repeat_count","interval_ms","speech_rate");integer(p.get("repeat_count"),1,5);integer(p.get("interval_ms"),0,600000);
        Object rate=p.get("speech_rate");require(rate instanceof Number&&((Number)rate).doubleValue()>=0.5&&((Number)rate).doubleValue()<=2,"playback.speech_rate");
    }
    public static DictationSpec parse(JSONObject value,Set<String> capabilities)throws Exception {
        require(capabilities.contains(value.optInt("schema_version",0)==2?V2_CAPABILITY:CAPABILITY),"unsupported capability; upgrade TV");return parse(value);
    }
    private static DictationSpec parseV2(JSONObject v)throws Exception {
        keys(v,"schema_version","task_id","content_hash","config_revision","skill_revision","callback_registration_id","lessons","phases","audio_assets","playback");
        integer(v.get("schema_version"),2,2);integer(v.get("config_revision"),1,Integer.MAX_VALUE);integer(v.get("skill_revision"),1,Integer.MAX_VALUE);
        text(v.get("task_id"),128,true);text(v.get("callback_registration_id"),128,true);validateParametersV2(v.getJSONObject("playback"));
        JSONArray lessons=v.getJSONArray("lessons");require(lessons.length()>0&&lessons.length()<=50,"lessons length");Map<String,JSONObject> byId=new HashMap<>();
        for(int n=0;n<lessons.length();n++){
            JSONObject l=lessons.getJSONObject(n);keys(l,"lesson_id","source","two_targets","four_targets");String id=text(l.get("lesson_id"),128,true);require(!byId.containsKey(id),"duplicate lesson");byId.put(id,l);
            JSONObject source=l.getJSONObject("source");keys(source,"book_id","unit","number","revision");text(source.get("book_id"),128,true);for(String k:new String[]{"unit","number","revision"})integer(source.get(k),1,Integer.MAX_VALUE);
            targets(l.getJSONArray("two_targets"));targets(l.getJSONArray("four_targets"));
        }
        JSONArray assets=v.getJSONArray("audio_assets");require(assets.length()<=20000,"audio_assets length");Set<String> assetIds=new HashSet<>();
        for(int n=0;n<assets.length();n++){JSONObject a=assets.getJSONObject(n);keys(a,"asset_id","sha256","media_type");require(assetIds.add(text(a.get("asset_id"),128,true)),"duplicate asset");require(text(a.get("sha256"),64,false).matches("[0-9a-f]{64}"),"asset sha256");choice(a,"media_type","audio/pcm");}
        JSONArray phases=v.getJSONArray("phases");require(phases.length()>0&&phases.length()<=2,"phases length");Set<String> phaseIds=new HashSet<>(),types=new HashSet<>(),itemIds=new HashSet<>();int total=0;
        for(int n=0;n<phases.length();n++){
            JSONObject p=phases.getJSONObject(n);keys(p,"phase_id","type","items");require(phaseIds.add(text(p.get("phase_id"),128,true)),"duplicate phase");choice(p,"type","recognition","dictation");String kind=p.getString("type");require(types.add(kind),"duplicate phase type");boolean recognition=kind.equals("recognition");
            JSONArray items=p.getJSONArray("items");require(items.length()<=10000,"items length");Map<String,Set<String>> coverage=new HashMap<>();for(String lid:byId.keySet())coverage.put(lid,new HashSet<String>());
            for(int j=0;j<items.length();j++){
                JSONObject item=items.getJSONObject(j);keys(item,"item_id","lesson_id","text","covers","audio_asset_id");require(itemIds.add(text(item.get("item_id"),128,true)),"duplicate item");String lid=text(item.get("lesson_id"),128,true);require(byId.containsKey(lid),"unknown lesson");
                String word=text(item.get("text"),32,false);Set<String> target=targets(byId.get(lid).getJSONArray(recognition?"two_targets":"four_targets"));JSONArray covers=item.getJSONArray("covers");require(covers.length()>0,"empty covers");Set<String> local=new HashSet<>();
                for(int k=0;k<covers.length();k++){String c=text(covers.get(k),1,false);require(target.contains(c)&&word.contains(c)&&local.add(c),"invalid covers");coverage.get(lid).add(c);}
                if(recognition)require(item.isNull("audio_asset_id"),"recognition is display only");else require(assetIds.contains(text(item.get("audio_asset_id"),128,true)),"missing resource");total++;
            }
            for(String lid:byId.keySet())require(coverage.get(lid).equals(targets(byId.get(lid).getJSONArray(recognition?"two_targets":"four_targets"))),"incomplete target coverage");
        }
        require(total>0,"no executable items");require(v.toString().getBytes(StandardCharsets.UTF_8).length<=262144,"payload too large");String hash=text(v.get("content_hash"),64,false);require(hash.matches("[0-9a-f]{64}")&&hash.equals(contentHash(v)),"content_hash mismatch");return new DictationSpec(v);
    }
    public static String contentHash(JSONObject value)throws Exception {
        JSONObject snapshot=new JSONObject(value.toString());snapshot.remove("content_hash");
        byte[] digest=MessageDigest.getInstance("SHA-256").digest(canonical(snapshot).getBytes(StandardCharsets.UTF_8));StringBuilder s=new StringBuilder();for(byte b:digest)s.append(String.format(Locale.ROOT,"%02x",b&255));return s.toString();
    }
    static String quote(String s){
        StringBuilder b=new StringBuilder("\"");for(int i=0;i<s.length();i++){char c=s.charAt(i);switch(c){case '"':b.append("\\\"");break;case '\\':b.append("\\\\");break;case '\b':b.append("\\b");break;case '\f':b.append("\\f");break;case '\n':b.append("\\n");break;case '\r':b.append("\\r");break;case '\t':b.append("\\t");break;default:if(c<32)b.append(String.format(Locale.ROOT,"\\u%04x",(int)c));else b.append(c);}}return b.append('"').toString();
    }
    static String canonical(Object v)throws Exception {
        if(v instanceof JSONObject){JSONObject o=(JSONObject)v;TreeSet<String> keys=new TreeSet<>();Iterator<String> it=o.keys();while(it.hasNext())keys.add(it.next());List<String> out=new ArrayList<>();for(String k:keys)out.add(quote(k)+":"+canonical(o.get(k)));return "{"+join(out)+"}";}
        if(v instanceof JSONArray){JSONArray a=(JSONArray)v;List<String> out=new ArrayList<>();for(int i=0;i<a.length();i++)out.add(canonical(a.get(i)));return "["+join(out)+"]";}
        if(v instanceof String)return quote((String)v);if(v==JSONObject.NULL)return "null";return String.valueOf(v);
    }
    static String join(List<String> s){StringBuilder b=new StringBuilder();for(String v:s){if(b.length()>0)b.append(',');b.append(v);}return b.toString();}
}
