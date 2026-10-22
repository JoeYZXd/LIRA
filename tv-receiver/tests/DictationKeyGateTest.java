package org.lira.receiver;
public final class DictationKeyGateTest {
 public static void main(String[] args) {
  DictationKeyGate gate=new DictationKeyGate();
  if(!gate.accept(true,0,0)||gate.accept(true,0,100)||gate.accept(true,1,400)||!gate.accept(true,0,450)||!gate.accept(false,0,451)||gate.accept(false,1,500))throw new AssertionError("remote repeat/double confirm fence");
  System.out.println("Remote held key and double confirm do not advance multiple pages");
 }
}
