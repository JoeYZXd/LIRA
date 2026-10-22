package org.lira.receiver;
/** Physical key repeats and a double press must not confirm two pages. */
final class DictationKeyGate {
 private long lastConfirmation=Long.MIN_VALUE;
 boolean accept(boolean confirmation,int repeat,long now) {
  if(repeat>0)return false;
  if(confirmation&&lastConfirmation!=Long.MIN_VALUE&&now-lastConfirmation<350)return false;
  if(confirmation)lastConfirmation=now;
  return true;
 }
}
