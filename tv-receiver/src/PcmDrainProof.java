package org.lira.receiver;
/** Shared receipt predicate: submitted bytes alone never mean audible completion. */
public final class PcmDrainProof {
 public static boolean completed(long expected,long played,int route,boolean muted,int volume,boolean owned){return expected>0&&played>=expected&&route==2&&!muted&&volume>0&&owned;}
}
