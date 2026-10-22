package org.lira.receiver;
import org.json.*;
/** Child-facing copy only. Never includes the dictated answer or global item cursor. */
public final class DictationPresentation {
 public static String number(int n){String[] digits={"零","一","二","三","四","五","六","七","八","九"};if(n<10)return digits[Math.max(0,n)];if(n<100)return (n<20?"":digits[n/10])+"十"+(n%10==0?"":digits[n%10]);return String.valueOf(n);}
 public static JSONObject describe(JSONObject s)throws Exception{
  JSONObject current=s.getJSONArray("steps").getJSONObject(s.getInt("index"));String state=s.getString("state"),kind=current.getString("kind");boolean recognition=kind.equals("recognition");int count=0,ordinal=0;
  JSONArray steps=s.getJSONArray("steps");for(int i=0;i<steps.length();i++)if(steps.getJSONObject(i).getString("kind").equals(kind)){count++;if(i<=s.getInt("index"))ordinal++;}
  String title=recognition?"读一读，认识新朋友":"轮到第"+number(ordinal)+"个词啦",subtitle=recognition?"把这一屏的词语，大声读给自己听。":"一共"+number(count)+"个词，慢慢写，不着急。";
  String left=recognition?"上一屏":"上一词，再听听",right=recognition?"下一屏":"写好了，下一个",center=recognition?"":"没听清，再听一次";
  if(recognition&&state.equals("waiting_child")){int after=s.optInt("page_end",s.getInt("index"))+1;if(after>=steps.length())right="读完啦，完成练习";else if(!steps.getJSONObject(after).getString("kind").equals("recognition"))right="读完了，开始听写";}
  if(state.equals("waiting_start")){title="准备好小本子了吗？";subtitle=recognition?"先读一读词语，再开始听写。":"拿好铅笔，坐端正，我们一起听写吧。";left="";right=recognition?"准备好，开始吧":"";center=recognition?"":"准备好，开始吧";}
  else if(state.equals("paused")){title="休息一下，等你回来";subtitle="准备好了，我们从刚才的地方继续。";left="";right=recognition?"准备好了，继续":"";center=recognition?"":"准备好了，继续";}
  else if(state.equals("playing")){title="小耳朵，认真听";subtitle="正在听写第"+number(ordinal)+"个词，一共"+number(count)+"个词。";}
  else if(state.equals("failed_item")){title="声音刚刚没跟上";subtitle="别着急，再听一次就好。";}
  else if(state.equals("completed")||state.equals("cancelled")){title=state.equals("completed")?"今天的练习完成啦！":"这次练习先到这里";subtitle=state.equals("completed")?"辛苦啦！把本子交给爸爸妈妈看看吧。":"收好本子，想练习的时候再来找我。";left="";right="";center="";}
  else if(!recognition){title="第"+number(ordinal)+"个词，慢慢写";subtitle="写好了按右键。想再听一遍，就按确认键。";}
  String progress=recognition?"词语认读小热身":"正在练习第"+number(ordinal)+"个词 · 一共"+number(count)+"个词";
  if(state.equals("waiting_start")||state.equals("completed")||state.equals("cancelled"))progress="读一读 · 听一听 · 写一写";
  return new JSONObject().put("title",title).put("subtitle",subtitle).put("progress",progress).put("left",left).put("right",right).put("center",center).put("recognition",recognition).put("ordinal",ordinal).put("total",count).put("state",state);
 }
}
