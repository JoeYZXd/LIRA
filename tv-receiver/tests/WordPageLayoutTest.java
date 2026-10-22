package org.lira.receiver;
import org.json.*;
public final class WordPageLayoutTest {
 public static void main(String[] args)throws Exception {JSONArray words=new JSONArray();for(String s:new String[]{"山川","黄河","依靠","尽头","千里"})words.put(new JSONObject().put("text",s));for(int[] size:new int[][]{{1920,1080},{1280,720},{720,480}}){JSONArray pages=WordPageLayout.pages(words,size[0],size[1]);int seen=0;for(int i=0;i<pages.length();i++){JSONArray page=pages.getJSONArray(i);if(page.length()==0)throw new AssertionError();for(int j=0;j<page.length();j++){if(!page.getJSONObject(j).getString("text").equals(words.getJSONObject(seen++).getString("text")))throw new AssertionError("word split/order");}}if(seen!=5)throw new AssertionError("word lost");}System.out.println("WordPageLayout whole-word, short and final pages across resolutions passed");}
}
