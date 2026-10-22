package org.lira.receiver;
import org.json.*;
/** Pure planning in relative character units. Never splits a word across pages or rows. */
public final class WordPageLayout {
 public static JSONArray largePages(JSONArray words,int width,int height)throws Exception{
  JSONArray planned=pages(words,width,height),result=new JSONArray();int limit=width<900?2:4;
  for(int p=0;p<planned.length();p++){JSONArray source=planned.getJSONArray(p),part=new JSONArray();for(int i=0;i<source.length();i++){if(part.length()==limit){result.put(part);part=new JSONArray();}part.put(source.getJSONObject(i));}if(part.length()>0)result.put(part);}return result;
 }
 public static JSONArray pages(JSONArray words,int width,int height)throws Exception {
  if(width<320||height<200)throw new IllegalArgumentException("screen too small");
  double font=Math.max(24,height/5.0),capacity=(width-width*.10)/font;
  JSONArray pages=new JSONArray(),page=new JSONArray();double used=0;int row=0;
  for(int i=0;i<words.length();i++){
   JSONObject word=words.getJSONObject(i);double units=word.getString("text").length()*1.6+1;
   if(units*24>width*.9)throw new IllegalArgumentException("word cannot fit legibly");
   if(used>0&&used+units>capacity){row++;used=0;}
   if(row>=2){pages.put(page);page=new JSONArray();row=0;}
   page.put(word);used+=units;
  }if(page.length()>0)pages.put(page);return pages;
 }
}
