package org.lira.receiver;
import java.io.*;
import java.nio.file.*;
public class TaskAssetStoreTest {
 public static void main(String[] args)throws Exception {
  TaskAssetStore s=new TaskAssetStore(Files.createTempDirectory("assets").toFile());String h=new String(new char[64]).replace('\0','a');
  String sha="96a296d224f285c67bee93c30f8a309157f0daa35dc5b87e410b78630a09cfc7";
  s.begin("task",h,"asset",2,sha);s.append("task",h,"asset",0,new byte[]{0,0});s.commit("task",h,"asset");if(s.read("task",h,"asset").length()!=2)throw new AssertionError();
  if(!s.begin("task",h,"asset",2,sha))throw new AssertionError("lost start ACK recovery");s.commit("task",h,"asset");
  s.begin("task",h,"retry",2,sha);s.append("task",h,"retry",0,new byte[]{0,0});s.append("task",h,"retry",0,new byte[]{0,0});
  try{s.append("task",h,"retry",0,new byte[]{1,0});throw new AssertionError();}catch(IOException expected){}
  s.commit("task",h,"retry");s.commit("task",h,"retry");
  try{s.begin("../task",h,"asset",2,sha);throw new AssertionError();}catch(IllegalArgumentException expected){}
  s.begin("task",h,"bad",4,sha);s.append("task",h,"bad",0,new byte[]{0,0});try{s.commit("task",h,"bad");throw new AssertionError();}catch(IOException expected){}
  try{s.read("task",h,"bad");throw new AssertionError();}catch(IOException expected){}
  s.release("task",h);System.out.println("TaskAssetStore: scoped bytes, digest and traversal checks passed");
 }
}
