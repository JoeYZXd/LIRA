package org.lira.receiver;
import android.view.*;import android.content.*;import android.graphics.*;import org.json.*;
/** Two rows, each whole word stays together; character spacing scales with available area. */
public final class WordPageView extends View {
 private final String[] words;private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);
 public WordPageView(Context c,JSONArray input)throws Exception {this(c,input,Color.WHITE);}
 public WordPageView(Context c,JSONArray input,int color)throws Exception {super(c);words=new String[input.length()];for(int i=0;i<words.length;i++)words[i]=input.getJSONObject(i).getString("text");paint.setColor(color);paint.setTextAlign(Paint.Align.CENTER);setContentDescription("二会字组词认读");}
 protected void onDraw(Canvas canvas){super.onDraw(canvas);if(words.length==1){paint.setTextSize(Math.min(getHeight()*.75f,getWidth()*.75f));while(paint.getTextSize()>24&&paint.measureText(words[0])>getWidth()*.90f)paint.setTextSize(paint.getTextSize()-2);Paint.FontMetrics fm=paint.getFontMetrics();canvas.drawText(words[0],getWidth()/2f,getHeight()/2f-(fm.ascent+fm.descent)/2,paint);return;}int split=(words.length+1)/2;float size=Math.min(getHeight()/2.65f,getWidth()/4f);for(;size>24;size-=2){paint.setTextSize(size);boolean fits=true;for(int row=0;row<2;row++){float width=0;for(int i=row*split;i<Math.min(words.length,(row+1)*split);i++)width+=paint.measureText(spaced(words[i]))+size;if(width>getWidth()-80)fits=false;}if(fits)break;}paint.setTextSize(size);for(int row=0;row<2;row++){int start=row*split,end=Math.min(words.length,start+split);if(end<=start)continue;float total=0;for(int i=start;i<end;i++)total+=paint.measureText(spaced(words[i]))+size;float x=(getWidth()-total+size)/2;for(int i=start;i<end;i++){String text=spaced(words[i]);float width=paint.measureText(text);canvas.drawText(text,x+width/2,getHeight()*(row==0?.36f:.76f),paint);x+=width+size;}}}
 static String spaced(String word){return word;}
}
