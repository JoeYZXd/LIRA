package org.lira.receiver;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import org.json.*;
public class DictationSpecTest {
    static void reject(JSONObject v)throws Exception {try{DictationSpec.parse(v);throw new AssertionError("accepted invalid");}catch(IllegalArgumentException|JSONException expected){}}
    public static void main(String[] args)throws Exception {
        JSONObject v=new JSONObject(new String(Files.readAllBytes(Paths.get(args[0])),StandardCharsets.UTF_8));
        DictationSpec.parse(v);
        JSONObject changed=new JSONObject(v.toString());changed.getJSONObject("parameters").put("expanded_word_seconds",45);reject(changed);
        changed.put("content_hash",DictationSpec.contentHash(changed));DictationSpec.parse(changed);
        changed.getJSONObject("parameters").remove("expanded_timer_anchor");reject(changed);
        changed=new JSONObject(v.toString());changed.getJSONArray("lessons").getJSONObject(0).getJSONArray("expanded_words").getJSONObject(0).put("covers",new JSONArray());reject(changed);
        changed=new JSONObject(v.toString());changed.put("schema_version",2);reject(changed);
        changed=new JSONObject(v.toString());changed.getJSONObject("parameters").put("expanded_repeat_count",true);reject(changed);
        changed=new JSONObject(v.toString());changed.getJSONArray("lessons").getJSONObject(0).put("recitation",new JSONObject().put("text","白日依山尽\n黄河入海流").put("prompt_audio_asset_id","asset-rec"));changed.put("content_hash",DictationSpec.contentHash(changed));DictationSpec.parse(changed);
        JSONObject v2=new JSONObject(new String(Files.readAllBytes(Paths.get(args[0]).resolveSibling("dictation-v2.json")),StandardCharsets.UTF_8));
        DictationSpec.parse(v2);
        JSONObject wordTask=new JSONObject(v2.toString());wordTask.getJSONArray("phases").getJSONObject(0).getJSONArray("items").getJSONObject(0).put("text","清水");wordTask.put("content_hash",DictationSpec.contentHash(wordTask));DictationSpec.parse(wordTask);
        JSONObject uncovered=new JSONObject(wordTask.toString());uncovered.getJSONArray("phases").getJSONObject(0).getJSONArray("items").getJSONObject(0).put("text","河水");uncovered.put("content_hash",DictationSpec.contentHash(uncovered));reject(uncovered);
        changed=new JSONObject(v2.toString());changed.getJSONArray("phases").getJSONObject(0).put("type","recitation");changed.put("content_hash",DictationSpec.contentHash(changed));reject(changed);
        changed=new JSONObject(v2.toString());changed.getJSONArray("phases").getJSONObject(1).getJSONArray("items").getJSONObject(0).put("covers",new JSONArray());changed.put("content_hash",DictationSpec.contentHash(changed));reject(changed);
        System.out.println("DictationSpec: shared fixture and rejection cases passed");
    }
}
