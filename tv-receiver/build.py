"""Build the API-23 receiver with the installed Android SDK and JDK (no Gradle download)."""
from pathlib import Path
import json, os, secrets, shutil, subprocess, zipfile

ROOT=Path(__file__).resolve().parent
def build():
    sdk=Path(os.environ.get('ANDROID_HOME','D:/tools/Android/sdk'))
    if sdk.name=='platform-tools':sdk=sdk.parent
    bt=sdk/'build-tools'/os.environ.get('LIRA_ANDROID_BUILD_TOOLS','35.0.0')
    jar=sdk/'platforms/android-33/android.jar'
    out=ROOT/'build';out.mkdir(exist_ok=True)
    for name in ['classes','dex']:
        folder=out/name
        if folder.exists():shutil.rmtree(folder)
        folder.mkdir()
    config=out/'client.json'
    if not config.exists():config.write_text(json.dumps({'host':'192.168.3.25','token':secrets.token_hex(32)}),encoding='utf-8')
    token=json.loads(config.read_text(encoding='utf-8'))['token']
    if len(token)!=64 or any(c not in '0123456789abcdef' for c in token):raise ValueError('Invalid pairing token')
    (out/'Pairing.java').write_text('package org.lira.receiver; final class Pairing { static final String TOKEN="'+token+'"; }',encoding='utf-8')
    def run(args):
        r=subprocess.run([str(x) for x in args],cwd=ROOT,capture_output=True,encoding='utf-8',errors='replace')
        if r.returncode:raise RuntimeError(r.stdout+r.stderr)
    run(['javac','-encoding','UTF-8','-source','8','-target','8','-bootclasspath',jar,'-d',out/'classes',*list((ROOT/'src').glob('*.java')),out/'Pairing.java'])
    run([bt/'d8.bat','--min-api','23','--lib',jar,'--output',out/'dex',*list((out/'classes').rglob('*.class'))])
    run([bt/'aapt.exe','package','-f','-M',ROOT/'AndroidManifest.xml','-I',jar,'-F',out/'unsigned.apk'])
    with zipfile.ZipFile(out/'unsigned.apk','a') as z:z.write(out/'dex/classes.dex','classes.dex')
    run([bt/'zipalign.exe','-f','4',out/'unsigned.apk',out/'aligned.apk'])
    if not (out/'signing.jks').exists():
        run(['keytool','-genkeypair','-keystore',out/'signing.jks','-storepass','android','-keypass','android','-alias','lira','-dname','CN=LIRA local receiver','-keyalg','RSA','-validity','3650'])
    run([bt/'apksigner.bat','sign','--ks',out/'signing.jks','--ks-pass','pass:android','--key-pass','pass:android','--out',out/'lira-tv.apk',out/'aligned.apk'])
    run([bt/'apksigner.bat','verify',out/'lira-tv.apk'])
    print('Built and signature-verified:',out/'lira-tv.apk')
if __name__=='__main__':build()
