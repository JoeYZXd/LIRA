package org.lira.receiver;
import android.content.Context;import android.graphics.*;import android.graphics.drawable.GradientDrawable;import android.view.*;import android.widget.*;import org.json.*;
/** Calm, large-format native learning surface for a child watching from a sofa. */
public final class LearningTaskView extends LinearLayout {
 static final int INK=Color.rgb(25,58,55),TEAL=Color.rgb(23,106,98),MUTED=Color.rgb(81,107,102),CREAM=Color.rgb(247,248,242),LINE=Color.rgb(218,230,220),PALE=Color.rgb(230,243,235),AMBER=Color.rgb(243,183,91);
 private final Context context;private boolean recognition;
 int dp(float n){return (int)(n*getResources().getDisplayMetrics().density+.5f);}
 GradientDrawable shape(int color,int radius){GradientDrawable d=new GradientDrawable();d.setColor(color);d.setCornerRadius(dp(radius));d.setStroke(dp(1),LINE);return d;}
 TextView text(String value,int size,int color){TextView t=new TextView(context);t.setText(value);t.setTextSize(size);t.setTextColor(color);t.setFontFeatureSettings("kern");t.setLineSpacing(dp(3),1.08f);t.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_YES);return t;}
 void gap(LinearLayout parent,int n){parent.addView(new View(context),new LayoutParams(1,dp(n)));}
 public LearningTaskView(Context c,JSONObject state,JSONArray words)throws Exception{
  super(c);context=c;JSONObject p=DictationPresentation.describe(state);String status=p.getString("state");boolean rec=p.getBoolean("recognition"),debug=state.optBoolean("debug_enabled");recognition=rec;setOrientation(VERTICAL);setBackgroundColor(CREAM);setPadding(dp(32),dp(22),dp(32),dp(20));
  LinearLayout header=new LinearLayout(c);header.setGravity(Gravity.CENTER_VERTICAL);TextView brand=text("LIRA  语文小课堂",20,TEAL);brand.setTypeface(null,Typeface.BOLD);header.addView(brand,new LayoutParams(0,dp(40),1));TextView stage=text(rec?"认读小热身":"听写小挑战",18,INK);stage.setPadding(dp(18),dp(6),dp(18),dp(6));stage.setBackground(shape(rec?PALE:Color.rgb(255,236,201),18));header.addView(stage);addView(header);gap(this,12);
  LinearLayout card=new LinearLayout(c);card.setOrientation(VERTICAL);card.setGravity(Gravity.CENTER);card.setPadding(dp(26),dp(16),dp(26),dp(16));card.setBackground(shape(Color.WHITE,24));addView(card,new LayoutParams(-1,0,1));
  TextView title=text(p.getString("title"),rec&&words.length()>0?26:52,INK);title.setTypeface(null,Typeface.BOLD);title.setGravity(Gravity.CENTER);card.addView(title,new LayoutParams(-1,-2));gap(card,7);
  TextView subtitle=text(p.getString("subtitle"),rec?20:28,MUTED);subtitle.setGravity(Gravity.CENTER);card.addView(subtitle,new LayoutParams(-1,-2));
  if(rec&&words.length()>0){card.addView(new WordPageView(c,words,INK),new LayoutParams(-1,0,1));}
  else{SoundIllustration illustration=new SoundIllustration(c,status);card.addView(illustration,new LayoutParams(-1,0,1));}
  if(!rec||words.length()==0){TextView progress=text(p.getString("progress"),26,TEAL);progress.setGravity(Gravity.CENTER);card.addView(progress,new LayoutParams(-1,-2));}
  if(!rec&&!status.equals("waiting_start")&&!status.equals("completed")&&!status.equals("cancelled"))card.addView(new Trail(c,p.getInt("ordinal"),p.getInt("total")),new LayoutParams(-1,dp(30)));
  gap(this,12);LinearLayout guides=new LinearLayout(c);guides.setGravity(Gravity.CENTER);
  addGuide(guides,"← 左键",p.getString("left"));addGuide(guides,"确认键",p.getString("center"));addGuide(guides,"右键 →",p.getString("right"));addView(guides,new LayoutParams(-1,-2));
  if(!status.equals("completed")&&!status.equals("cancelled")){TextView help=text("想休息一下？按返回键暂停 · 长按返回键结束练习",20,MUTED);help.setGravity(Gravity.CENTER);help.setPadding(0,dp(7),0,0);addView(help,new LayoutParams(-1,-2));}
  if(debug){TextView log=text(safeLog(state),12,Color.rgb(212,237,222));log.setBackground(shape(INK,10));log.setPadding(dp(12),dp(6),dp(12),dp(6));log.setMaxLines(5);LayoutParams lp=new LayoutParams(-1,dp(88));lp.topMargin=dp(8);addView(log,lp);}
 }
 private void addGuide(LinearLayout row,String key,String action){if(action.isEmpty())return;LinearLayout box=new LinearLayout(context);box.setOrientation(VERTICAL);box.setGravity(Gravity.CENTER);box.setPadding(dp(12),dp(8),dp(12),dp(8));box.setBackground(shape(Color.WHITE,16));if(recognition){TextView label=text(key+"  "+action,26,INK);label.setGravity(Gravity.CENTER);box.addView(label);LayoutParams lp=new LayoutParams(0,-2,1);lp.leftMargin=dp(5);lp.rightMargin=dp(5);row.addView(box,lp);return;}TextView k=text(key,22,TEAL);k.setTypeface(null,Typeface.BOLD);box.addView(k);TextView label=text(action,28,INK);box.addView(label);LayoutParams lp=new LayoutParams(0,-2,1);lp.leftMargin=dp(5);lp.rightMargin=dp(5);row.addView(box,lp);}
 private String safeLog(JSONObject s)throws Exception{StringBuilder b=new StringBuilder("调试模式 · 仅供家长排查");JSONArray rows=s.optJSONArray("safe_log");if(rows!=null)for(int n=Math.max(0,rows.length()-3);n<rows.length();n++){JSONObject e=rows.getJSONObject(n);b.append("\n#").append(e.getLong("sequence")).append(" ").append(e.getString("action")).append(" · ").append(e.getString("state")).append(" · 播报 ").append(e.getInt("actual_repeats"));}return b.toString();}
 private static final class SoundIllustration extends View{
  final Paint p=new Paint(Paint.ANTI_ALIAS_FLAG);final String state;SoundIllustration(Context c,String s){super(c);state=s;setImportantForAccessibility(IMPORTANT_FOR_ACCESSIBILITY_NO);}
  protected void onDraw(Canvas canvas){super.onDraw(canvas);float size=Math.min(getWidth(),getHeight())*.62f,cx=getWidth()/2f,cy=getHeight()/2f,r=size/2; p.setStyle(Paint.Style.FILL);p.setColor(PALE);canvas.drawCircle(cx,cy,r,p);p.setColor(TEAL);p.setStyle(Paint.Style.STROKE);p.setStrokeWidth(Math.max(3,r*.055f));p.setStrokeCap(Paint.Cap.ROUND);p.setStrokeJoin(Paint.Join.ROUND);
   Path book=new Path();book.moveTo(cx,cy+r*.4f);book.lineTo(cx-r*.5f,cy+r*.25f);book.lineTo(cx-r*.5f,cy-r*.33f);book.quadTo(cx-r*.24f,cy-r*.43f,cx,cy-r*.25f);book.quadTo(cx+r*.24f,cy-r*.43f,cx+r*.5f,cy-r*.33f);book.lineTo(cx+r*.5f,cy+r*.25f);book.close();canvas.drawPath(book,p);canvas.drawLine(cx,cy-r*.25f,cx,cy+r*.4f,p);p.setStyle(Paint.Style.FILL);p.setColor(AMBER);canvas.drawCircle(cx+r*.58f,cy-r*.48f,r*.19f,p);
  }
 }
 private static final class Trail extends View{
  final int current,total;final Paint p=new Paint(Paint.ANTI_ALIAS_FLAG);Trail(Context c,int n,int t){super(c);current=n;total=t;setContentDescription("正在练习第"+n+"个词，一共"+t+"个词");}
  protected void onDraw(Canvas canvas){int visible=Math.min(total,12);if(visible<1)return;float gap=Math.min(getWidth()/(visible+2f),32*getResources().getDisplayMetrics().density),start=(getWidth()-(visible-1)*gap)/2f,r=Math.min(getHeight()*.16f,gap*.18f);int index=(int)Math.ceil(current*visible/(double)total);for(int i=0;i<visible;i++){p.setColor(i<index?TEAL:LINE);canvas.drawCircle(start+i*gap,getHeight()/2f,i==index-1?r*1.3f:r,p);}}
 }
}
