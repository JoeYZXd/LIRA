package org.lira.receiver;
import java.io.*;
import org.json.*;
/** One crash-consistent checkpoint contains state and the unacknowledged event outbox. */
public final class DictationStateStore {
 private final File file;
 public DictationStateStore(File file){this.file=file;}
 private File[] snapshots(){File[] all=file.getParentFile().listFiles(new FilenameFilter(){public boolean accept(File d,String n){return n.matches(java.util.regex.Pattern.quote(file.getName())+"\\.[0-9]{16}");}});if(all==null)return new File[0];java.util.Arrays.sort(all);return all;}
 public synchronized JSONObject load()throws Exception {File[] all=snapshots();File source=all.length>0?all[all.length-1]:file;if(!source.exists())return null;ByteArrayOutputStream b=new ByteArrayOutputStream();try(FileInputStream in=new FileInputStream(source)){byte[] c=new byte[8192];int n;while((n=in.read(c))!=-1){b.write(c,0,n);if(b.size()>4194304)throw new IOException("checkpoint too large");}}return new JSONObject(new String(b.toByteArray(),"UTF-8"));}
 public synchronized void save(JSONObject value)throws Exception {File parent=file.getParentFile();if(!parent.exists()&&!parent.mkdirs())throw new IOException("checkpoint directory");File[] all=snapshots();long sequence=all.length==0?1:Long.parseLong(all[all.length-1].getName().substring(file.getName().length()+1))+1;File target=new File(file.getPath()+String.format(java.util.Locale.ROOT,".%016d",sequence));File temp=new File(file.getPath()+".tmp");try(FileOutputStream out=new FileOutputStream(temp)){out.write(value.toString().getBytes("UTF-8"));out.getFD().sync();}if(!temp.renameTo(target))throw new IOException("checkpoint rename");for(int i=0;i<all.length-1;i++)all[i].delete();}
}
