package org.lira.receiver;
public final class TaskAudioPlayerTest {
 public static void main(String[] args){if(PcmDrainProof.completed(24000,23999,2,false,4,true)||PcmDrainProof.completed(24000,24000,11,false,4,true)||PcmDrainProof.completed(24000,24000,2,true,4,true)||PcmDrainProof.completed(24000,24000,2,false,0,true)||PcmDrainProof.completed(24000,24000,2,false,4,false)||!PcmDrainProof.completed(24000,24000,2,false,4,true))throw new AssertionError("PCM drain proof");System.out.println("TaskAudioPlayer receipt: route, mute, volume, ownership and playback head passed (hardware not simulated)");}
}
