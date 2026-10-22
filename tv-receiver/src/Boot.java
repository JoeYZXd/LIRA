package org.lira.receiver;
import android.content.*;
public class Boot extends BroadcastReceiver {
    @Override public void onReceive(Context c, Intent i) {
        if (Intent.ACTION_BOOT_COMPLETED.equals(i.getAction()))
            c.startService(new Intent(c, ReceiverService.class));
    }
}
