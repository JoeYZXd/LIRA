package org.lira.receiver;

import java.io.*;
import java.security.MessageDigest;
import java.util.*;

/** No playback: bounded, task/hash-scoped PCM cache. Caller authenticates requests. */
public final class TaskAssetStore {
    public static final long MAX_ASSET=5760000L, MAX_TASK=67108864L;
    private final File root;
    private final Map<String,Pending> uploads=new HashMap<String,Pending>();
    private final Map<String,String> completed=new HashMap<String,String>();
    private static final class Pending { File file; long bytes; String digest; Pending(File f,long b,String d){file=f;bytes=b;digest=d;} }
    public TaskAssetStore(File directory)throws IOException {root=directory.getCanonicalFile();if(!root.exists()&&!root.mkdirs())throw new IOException("asset directory unavailable");}
    private static void id(String v){if(v==null||!v.matches("[A-Za-z0-9_.:-]{1,128}")||v.contains(".."))throw new IllegalArgumentException("unsafe asset id");}
    private File folder(String task,String hash)throws IOException {id(task);if(hash==null||!hash.matches("[0-9a-f]{64}"))throw new IllegalArgumentException("invalid task hash");File f=new File(root,task+"-"+hash).getCanonicalFile();if(!f.getParentFile().equals(root))throw new IOException("outside cache");return f;}
    private File asset(String task,String hash,String aid,String suffix)throws IOException {id(aid);File dir=folder(task,hash);File f=new File(dir,aid+suffix).getCanonicalFile();if(!f.getParentFile().equals(dir))throw new IOException("outside task");return f;}
    private long size(File file){if(!file.exists())return 0;if(file.isFile())return file.length();long n=0;File[] children=file.listFiles();if(children!=null)for(File c:children)n+=size(c);return n;}
    private void remove(File f)throws IOException {File[] children=f.listFiles();if(children!=null)for(File c:children)remove(c);if(f.exists()&&!f.delete())throw new IOException("cache removal failed");}
    public synchronized boolean begin(String task,String hash,String aid,long bytes,String sha)throws IOException {
        if(bytes<=0||bytes>MAX_ASSET||bytes%2!=0||sha==null||!sha.matches("[0-9a-f]{64}"))throw new IllegalArgumentException("invalid PCM descriptor");
        File dir=folder(task,hash);
        File[] dirs=root.listFiles();if(dirs!=null&&dirs.length>=4&&!dir.exists())throw new IOException("task cache full");
        if(!dir.exists()&&!dir.mkdirs())throw new IOException("task directory unavailable");
        File out=asset(task,hash,aid,".part");String key=out.getPath();
        File ready=asset(task,hash,aid,".pcm");
        if(ready.exists()){
            if(ready.length()!=bytes||!digest(ready).equals(sha))throw new IOException("immutable asset mismatch");
            completed.put(key,sha);return true;
        }
        Pending active=uploads.get(key);
        if(active!=null){if(active.bytes!=bytes||!active.digest.equals(sha))throw new IOException("upload descriptor conflict");new FileOutputStream(out,false).close();return false;}
        long reserved=0;for(Pending p:uploads.values())if(p.file.getParentFile().equals(dir))reserved+=p.bytes-p.file.length();
        if(size(dir)+reserved+bytes>MAX_TASK)throw new IOException("task quota exceeded");
        new FileOutputStream(out,false).close();uploads.put(key,new Pending(out,bytes,sha));return false;
    }
    public synchronized void append(String task,String hash,String aid,long offset,byte[] pcm)throws IOException {
        File f=asset(task,hash,aid,".part");Pending p=uploads.get(f.getPath());
        if(p==null||pcm==null||pcm.length==0||pcm.length>24000||pcm.length%2!=0||offset<0||offset+pcm.length>p.bytes)throw new IOException("invalid upload chunk");
        if(offset<f.length()){
            if(offset+pcm.length>f.length())throw new IOException("overlapping upload");
            RandomAccessFile read=new RandomAccessFile(f,"r");try{read.seek(offset);byte[] old=new byte[pcm.length];read.readFully(old);if(!Arrays.equals(old,pcm))throw new IOException("conflicting duplicate chunk");}finally{read.close();}return;
        }
        if(offset!=f.length())throw new IOException("noncontiguous upload");
        FileOutputStream stream=new FileOutputStream(f,true);try{stream.write(pcm);}finally{stream.close();}
    }
    private String digest(File f)throws IOException {
        MessageDigest md;try{md=MessageDigest.getInstance("SHA-256");}catch(Exception e){throw new IOException(e);}
        FileInputStream in=new FileInputStream(f);try{byte[] b=new byte[8192];int n;while((n=in.read(b))!=-1)md.update(b,0,n);}finally{in.close();}
        StringBuilder hex=new StringBuilder();for(byte b:md.digest())hex.append(String.format("%02x",b&255));
        return hex.toString();
    }
    public synchronized File commit(String task,String hash,String aid)throws Exception {
        File f=asset(task,hash,aid,".part");Pending p=uploads.get(f.getPath());
        if(p==null){File ready=asset(task,hash,aid,".pcm");String sha=completed.get(f.getPath());if(sha!=null&&ready.isFile()&&digest(ready).equals(sha))return ready;throw new IOException("unknown upload");}
        if(f.length()!=p.bytes||!digest(f).equals(p.digest)){uploads.remove(f.getPath());f.delete();throw new IOException("asset checksum mismatch");}
        File result=asset(task,hash,aid,".pcm");if(result.exists())throw new IOException("immutable asset exists");if(!f.renameTo(result))throw new IOException("commit failed");uploads.remove(f.getPath());completed.put(f.getPath(),p.digest);return result;
    }
    public synchronized File read(String task,String hash,String aid)throws IOException {File f=asset(task,hash,aid,".pcm");if(!f.isFile()||f.length()==0)throw new IOException("asset missing");return f;}
    public synchronized void release(String task,String hash)throws IOException {File dir=folder(task,hash);Iterator<Map.Entry<String,Pending>> it=uploads.entrySet().iterator();while(it.hasNext())if(it.next().getValue().file.getParentFile().equals(dir))it.remove();Iterator<String> done=completed.keySet().iterator();while(done.hasNext())if(new File(done.next()).getParentFile().equals(dir))done.remove();remove(dir);}
}
