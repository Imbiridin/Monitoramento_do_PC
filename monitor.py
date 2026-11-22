import os, re, time, json, threading, subprocess, csv, shutil, urllib.request
from collections import deque
import psutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTA = 8000
DISCO = "C:\\" if os.name == "nt" else "/"
N = 60  # segundos de histórico nos gráficos

est = {
    "cpu": deque([0.0] * N, maxlen=N), "ram": deque([0.0] * N, maxlen=N),
    "down": deque([0.0] * N, maxlen=N), "up": deque([0.0] * N, maxlen=N),
    "cpus": [0.0],
}

# ---------------- GPU e FPS ----------------
# GPU AMD (e Intel): lê os sensores do LibreHardwareMonitor (servidor web ligado, porta 8085).
LHM_URL = "http://127.0.0.1:8085/data.json"
# GPU NVIDIA: usa o nvidia-smi (vem com o driver) quando existir.
NVSMI = shutil.which("nvidia-smi")
if not NVSMI and os.path.exists(r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
    NVSMI = r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"
# FPS: usa o PresentMon (baixe em github.com/GameTechDev/PresentMon/releases) e coloque aqui:
PRESENTMON = r"C:\monitor\PresentMon.exe"
# Se quiser fixar um jogo, escreva o nome do .exe (ex.: "eldenring.exe"). Vazio = detecta sozinho.
JOGO = ""
SEM_JANELA = 0x08000000 if os.name == "nt" else 0
IGNORAR = set(["dwm.exe", "explorer.exe", "<unknown>", "<error>", "searchhost.exe", "shellexperiencehost.exe",
               "startmenuexperiencehost.exe", "textinputhost.exe", "applicationframehost.exe", "taskmgr.exe",
               "python.exe", "pythonw.exe", "msedgewebview2.exe", "systemsettings.exe"])

gpu = {"d": None, "hist": deque([0.0] * N, maxlen=N), "msg": ""}
fps = {"msg": "", "frames": {}}

def num(t):
    t = t.strip()
    try:
        return float(t)
    except ValueError:
        return None

def le_gpu():
    r = subprocess.run([NVSMI, "--query-gpu=name,temperature.gpu,power.draw,power.limit,utilization.gpu,memory.used,memory.total",
                        "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=4, creationflags=SEM_JANELA)
    p = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
    return {"nome": p[0], "temp": num(p[1]), "w": num(p[2]), "lim": num(p[3]), "uso": num(p[4]),
            "vu": num(p[5]), "vt": num(p[6])}

def valor(no):
    try:
        return float(no.get("RawValue"))
    except (TypeError, ValueError):
        m = re.search(r"-?\d+(\.\d+)?", str(no.get("Value", "")).replace(",", "."))
        return float(m.group()) if m else None

def acha_gpus(no, achado):
    if str(no.get("HardwareId", "")).startswith("/gpu-"):
        achado.append(no)
        return
    for f in no.get("Children", []):
        acha_gpus(f, achado)

def sensores(no, out):
    sid = no.get("SensorId")
    if sid:
        partes = sid.strip("/").split("/")
        if len(partes) >= 3:
            out.append((partes[2], no.get("Text", ""), valor(no)))
    for f in no.get("Children", []):
        sensores(f, out)

def le_gpu_lhm():
    with urllib.request.urlopen(LHM_URL, timeout=3) as r:
        raiz = json.loads(r.read().decode("utf-8"))
    placas = []
    acha_gpus(raiz, placas)
    if not placas:
        raise RuntimeError("sem GPU")
    placas.sort(key=lambda p: 1 if "integrated" in str(p.get("HardwareId", "")) else 0)
    hw = placas[0]
    sens = []
    sensores(hw, sens)

    def pega(tipos, prefere, estrito=False):
        lista = [(n.lower(), v) for t, n, v in sens if t in tipos and v is not None]
        for p in prefere:
            for n, v in lista:
                if p in n:
                    return v
        return None if estrito or not lista else lista[0][1]

    return {"nome": hw.get("Text", "GPU"),
            "temp": pega(["temperature"], ["gpu core", "core", "edge"]),
            "w": pega(["power"], ["package", "total", "board", "gpu core"]),
            "lim": None,
            "uso": pega(["load"], ["gpu core", "d3d 3d"]),
            "vu": pega(["smalldata", "data"], ["memory used"], True),
            "vt": pega(["smalldata", "data"], ["memory total"], True)}

def thread_gpu():
    while True:
        try:
            gpu["d"] = le_gpu() if NVSMI else le_gpu_lhm()
            gpu["msg"] = ""
            gpu["hist"].append(gpu["d"]["uso"] or 0)
        except Exception:
            gpu["d"] = None
            gpu["msg"] = ("nvidia-smi falhou" if NVSMI else
                          "Abra o LibreHardwareMonitor e ative Options > Remote Web Server > Run")
        time.sleep(1)

def thread_fps():
    if not os.path.exists(PRESENTMON):
        fps["msg"] = "PresentMon não encontrado em " + PRESENTMON
        return
    cmd = [PRESENTMON, "--output_stdout", "--no_console_stats", "--v1_metrics", "--session_name", "monitor_pc"]
    if JOGO:
        cmd += ["--process_name", JOGO]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                             creationflags=SEM_JANELA)
    except Exception as e:
        fps["msg"] = "Não consegui abrir o PresentMon"
        return
    col = None
    for linha in p.stdout:
        linha = linha.strip()
        if not linha:
            continue
        campos = next(csv.reader([linha]))
        if col is None:
            if "Application" in campos:
                col = campos.index("Application")
                fps["msg"] = ""
            else:
                fps["msg"] = linha[:90]
            continue
        if len(campos) <= col:
            continue
        fps["frames"].setdefault(campos[col], deque()).append(time.time())
    fps["msg"] = fps["msg"] or "PresentMon parou (rode o Terminal como administrador)"

def calcula_fps():
    agora, melhor = time.time(), None
    for app, dq in list(fps["frames"].items()):
        while dq and agora - dq[0] > 1.0:
            dq.popleft()
        if app.lower() in IGNORAR or not dq:
            continue
        if melhor is None or len(dq) > melhor[1]:
            melhor = (app, len(dq))
    return melhor

def coletor():
    ant = psutil.net_io_counters()
    while True:
        cpus = psutil.cpu_percent(interval=1, percpu=True)
        atual = psutil.net_io_counters()
        est["down"].append((atual.bytes_recv - ant.bytes_recv) * 8 / 1e6)
        est["up"].append((atual.bytes_sent - ant.bytes_sent) * 8 / 1e6)
        ant = atual
        est["cpus"] = cpus
        est["cpu"].append(sum(cpus) / len(cpus))
        est["ram"].append(psutil.virtual_memory().percent)

def dados():
    m = psutil.virtual_memory()
    du = psutil.disk_usage(DISCO)
    s = int(time.time() - psutil.boot_time())
    r = lambda lista: [round(x, 1) for x in lista]
    d = {
        "hora": time.strftime("%H:%M:%S"),
        "ligado": "ligado há %dh %02dmin" % (s // 3600, s % 3600 // 60),
        "cpu": round(est["cpu"][-1], 1), "hcpu": r(est["cpu"]), "hram": r(est["ram"]),
        "cpus": [round(c) for c in est["cpus"]],
        "ram": m.percent, "ramu": round(m.used / 2 ** 30, 1), "ramt": round(m.total / 2 ** 30, 1),
        "disco": du.percent, "discou": round(du.used / 2 ** 30), "discot": round(du.total / 2 ** 30),
        "discon": DISCO,
        "down": round(est["down"][-1], 1), "up": round(est["up"][-1], 1),
        "hdown": r(est["down"]), "hup": r(est["up"]),
    }
    d["gpu"] = gpu["d"]
    d["hgpu"] = r(gpu["hist"])
    f = calcula_fps()
    d["fps"] = f[1] if f else None
    d["fapp"] = f[0] if f else ""
    d["fmsg"] = fps["msg"]
    d["gmsg"] = gpu["msg"]
    try:
        for lista in psutil.sensors_temperatures().values():
            if lista:
                d["temp"] = round(lista[0].current)
                break
    except Exception:
        pass
    return d

HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Monitor do PC</title>
<style>
*{-webkit-box-sizing:border-box;box-sizing:border-box}
html,body{margin:0;padding:0}
body{padding:14px;font-family:sans-serif;color:#e8eefc;background:#0b0f19;
background:-webkit-linear-gradient(top,#0b0f19,#131b2e);background:linear-gradient(#0b0f19,#131b2e);min-height:100%}
.top{overflow:hidden;margin-bottom:14px}
.top .l{float:left}.top .r{float:right;text-align:right}
.tit{font-size:26px;font-weight:bold;letter-spacing:1px}
.sub{font-size:14px;color:#7f8ea8;margin-top:2px}
.hora{font-size:30px;font-weight:bold;color:#38bdf8}
.row{overflow:hidden;margin-bottom:14px}
.card{background:#1a2338;background:-webkit-linear-gradient(top,#1e2840,#161e31);background:linear-gradient(#1e2840,#161e31);
border:1px solid #26324d;border-radius:14px;padding:14px;-webkit-box-shadow:0 3px 10px rgba(0,0,0,.45);box-shadow:0 3px 10px rgba(0,0,0,.45)}
.half{float:left;width:49%}.half.d{float:right}
.t{font-size:13px;color:#7f8ea8;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.big{font-size:52px;font-weight:bold;line-height:56px}
.mid{font-size:20px;margin-top:6px;color:#c3cde2}
.bar{height:12px;border-radius:6px;background:#0f1626;margin-top:10px;overflow:hidden}
.bar div{height:12px;border-radius:6px}
.gl{float:left;width:150px;height:150px}
.gr{overflow:hidden;padding-left:14px}
.nuc{display:block;float:left;width:25%;padding-right:10px;margin-bottom:10px;font-size:14px;color:#9fb0cc}
.nuc b{float:right;color:#e8eefc}
.nuc .bar{height:8px;margin-top:4px;clear:both}.nuc .bar div{height:8px}
#off{display:none;background:#7f1d1d;color:#fff;padding:8px;border-radius:8px;margin-bottom:12px;text-align:center}
canvas{display:block}
.tri{float:left;width:33.33%;padding-right:10px}.tri .big{font-size:46px}.u{font-size:18px;color:#7f8ea8;font-weight:normal}
.tri .sub{height:18px;overflow:hidden;white-space:nowrap}
.net{font-size:22px;font-weight:bold;margin-right:18px}
</style></head><body>
<div id="off">Sem conexão com o PC. Tentando de novo...</div>
<div class="top"><div class="l"><div class="tit">MONITOR DO PC</div><div class="sub" id="lig">...</div></div>
<div class="r"><div class="hora" id="hora">--:--:--</div><div class="sub" id="temp"></div></div></div>

<div class="row"><div class="card">
<div class="t">Processador</div>
<div class="gl"><canvas id="anel" width="150" height="150"></canvas></div>
<div class="gr"><div class="mid" id="cpuT" style="margin-top:0">Últimos 60 s</div><canvas id="gcpu" height="110" style="margin-top:8px"></canvas></div>
</div></div>

<div class="row"><div class="card">
<div class="t" id="gnome">Placa de vídeo</div>
<div class="tri"><div class="t" style="margin-bottom:0">Temperatura</div><div class="big" id="gtemp">--</div><div class="sub">&nbsp;</div></div>
<div class="tri"><div class="t" style="margin-bottom:0">Consumo</div><div class="big" id="gw">--</div><div class="sub" id="glim">&nbsp;</div></div>
<div class="tri"><div class="t" style="margin-bottom:0">FPS</div><div class="big" id="gfps">--</div><div class="sub" id="gapp">&nbsp;</div></div>
<div style="clear:both"></div>
<div class="mid" id="guso" style="margin-top:12px"></div><div class="bar"><div id="gusob"></div></div>
<div class="mid" id="gvram"></div><div class="bar"><div id="gvramb"></div></div>
<canvas id="ggpu" height="70" style="margin-top:12px"></canvas>
</div></div>

<div class="row"><div class="card half"><div class="t">Memória RAM</div><div class="big" id="ram">0%</div>
<div class="bar"><div id="ramb"></div></div><div class="mid" id="ramt"></div></div>
<div class="card half d"><div class="t" id="dt">Disco</div><div class="big" id="dis">0%</div>
<div class="bar"><div id="disb"></div></div><div class="mid" id="dist"></div></div>
</div>

<div class="row"><div class="card">
<div class="t">Rede</div>
<div><span class="net" style="color:#38bdf8" id="down">&#8595; 0</span><span class="net" style="color:#fb923c" id="up">&#8593; 0</span><span class="sub" id="pico"></span></div>
<canvas id="gnet" height="90" style="margin-top:8px"></canvas>
</div></div>

<div class="row"><div class="card"><div class="t">Núcleos</div><div id="nucs" style="overflow:hidden"></div></div></div>

<script>
function $(i){return document.getElementById(i);}
function ctemp(t){return t>=85?'#f87171':t>=75?'#fbbf24':'#34d399';}
function cfps(f){return f>=60?'#34d399':f>=30?'#fbbf24':'#f87171';}
function cor(p){return p>85?'#f87171':p>60?'#fbbf24':'#34d399';}

function anel(cv,p){
  var c=cv.getContext('2d'),w=cv.width,h=cv.height,r=w/2-16;
  c.clearRect(0,0,w,h);c.lineWidth=14;c.lineCap='round';
  c.strokeStyle='#0f1626';c.beginPath();c.arc(w/2,h/2,r,0.75*Math.PI,2.25*Math.PI,false);c.stroke();
  if(p>0.5){c.strokeStyle=cor(p);c.beginPath();c.arc(w/2,h/2,r,0.75*Math.PI,(0.75+1.5*p/100)*Math.PI,false);c.stroke();}
  c.fillStyle='#fff';c.textAlign='center';c.textBaseline='middle';
  c.font='bold 38px sans-serif';c.fillText(Math.round(p)+'%',w/2,h/2-4);
  c.fillStyle='#7f8ea8';c.font='13px sans-serif';c.fillText('CPU',w/2,h/2+26);
}

function graf(cv,series,max){
  var c=cv.getContext('2d'),w=cv.width,h=cv.height,i,k,y;
  c.clearRect(0,0,w,h);c.lineWidth=1;c.strokeStyle='#223049';
  for(k=1;k<4;k++){y=Math.round(h*k/4)+0.5;c.beginPath();c.moveTo(0,y);c.lineTo(w,y);c.stroke();}
  for(k=0;k<series.length;k++){
    var s=series[k],n=s.d.length,st=w/(n-1);
    c.beginPath();
    for(i=0;i<n;i++){y=h-2-(Math.min(s.d[i],max)/max)*(h-6);if(i==0){c.moveTo(0,y);}else{c.lineTo(i*st,y);}}
    c.lineTo(w,h);c.lineTo(0,h);c.closePath();c.fillStyle=s.fill;c.fill();
    c.beginPath();
    for(i=0;i<n;i++){y=h-2-(Math.min(s.d[i],max)/max)*(h-6);if(i==0){c.moveTo(0,y);}else{c.lineTo(i*st,y);}}
    c.lineWidth=2;c.strokeStyle=s.cor;c.stroke();c.lineWidth=1;
  }
}

function ajusta(){
  var a=['gcpu','gnet','ggpu'],i,cv;
  for(i=0;i<a.length;i++){cv=$(a[i]);cv.width=cv.parentNode.clientWidth-(a[i]=='gcpu'?14:28);}
  if(ult){desenha(ult);}
}

var ult=null;
function desenha(d){
  $('hora').innerHTML=d.hora;$('lig').innerHTML=d.ligado;
  $('temp').innerHTML=(typeof d.temp!=='undefined')?('Temp. '+d.temp+' °C'):'';
  anel($('anel'),d.cpu);
  graf($('gcpu'),[{d:d.hram,cor:'#a78bfa',fill:'rgba(167,139,250,0.10)'},{d:d.hcpu,cor:'#38bdf8',fill:'rgba(56,189,248,0.22)'}],100);
  $('cpuT').innerHTML='<span style="color:#38bdf8">&#9632; CPU</span> &nbsp; <span style="color:#a78bfa">&#9632; RAM</span>';
  $('ram').innerHTML=Math.round(d.ram)+'%';$('ram').style.color=cor(d.ram);
  $('ramb').style.width=d.ram+'%';$('ramb').style.background=cor(d.ram);
  $('ramt').innerHTML=d.ramu+' / '+d.ramt+' GB';
  $('dt').innerHTML='Disco '+d.discon;
  $('dis').innerHTML=Math.round(d.disco)+'%';$('dis').style.color=cor(d.disco);
  $('disb').style.width=d.disco+'%';$('disb').style.background=cor(d.disco);
  $('dist').innerHTML=d.discou+' / '+d.discot+' GB';
  $('down').innerHTML='&#8595; '+d.down+' Mbps';$('up').innerHTML='&#8593; '+d.up+' Mbps';
  var g=d.gpu;
  if(g){
    $('gnome').innerHTML='Placa de vídeo &nbsp;<span style="color:#e8eefc;text-transform:none">'+g.nome+'</span>';
    if(g.temp!==null){$('gtemp').innerHTML=Math.round(g.temp)+'<span class="u"> °C</span>';$('gtemp').style.color=ctemp(g.temp);}
    if(g.w!==null){$('gw').innerHTML=Math.round(g.w)+'<span class="u"> W</span>';}
    $('glim').innerHTML=g.lim!==null?('limite '+Math.round(g.lim)+' W'):'&nbsp;';
    if(g.uso!==null){$('guso').innerHTML='Uso da GPU: '+Math.round(g.uso)+'%';$('gusob').style.width=g.uso+'%';$('gusob').style.background=cor(g.uso);}
    if(g.vu!==null&&g.vt){var vp=g.vu/g.vt*100;$('gvram').innerHTML='VRAM: '+(g.vu/1024).toFixed(1)+' / '+(g.vt/1024).toFixed(1)+' GB';$('gvramb').style.width=vp+'%';$('gvramb').style.background=cor(vp);}
    graf($('ggpu'),[{d:d.hgpu,cor:'#34d399',fill:'rgba(52,211,153,0.18)'}],100);
  }else{
    $('gnome').innerHTML='Placa de vídeo &nbsp;<span style="color:#fbbf24;text-transform:none">'+d.gmsg+'</span>';
  }
  if(d.fps!==null){$('gfps').innerHTML=d.fps;$('gfps').style.color=cfps(d.fps);$('gapp').innerHTML=d.fapp;}
  else{$('gfps').innerHTML='--';$('gfps').style.color='#7f8ea8';$('gapp').innerHTML=d.fmsg?d.fmsg:'abra um jogo';}
  var mx=1,i;for(i=0;i<d.hdown.length;i++){mx=Math.max(mx,d.hdown[i],d.hup[i]);}
  $('pico').innerHTML='pico '+Math.round(mx)+' Mbps';
  graf($('gnet'),[{d:d.hup,cor:'#fb923c',fill:'rgba(251,146,60,0.15)'},{d:d.hdown,cor:'#38bdf8',fill:'rgba(56,189,248,0.22)'}],mx);
  var h='';for(i=0;i<d.cpus.length;i++){h+='<div class="nuc">N'+i+' <b>'+d.cpus[i]+'%</b><div class="bar"><div style="width:'+d.cpus[i]+'%;background:'+cor(d.cpus[i])+'"></div></div></div>';}
  $('nucs').innerHTML=h;
}

function pede(){
  var x=new XMLHttpRequest();
  x.open('GET','/dados?'+new Date().getTime(),true);
  x.onreadystatechange=function(){
    if(x.readyState==4){
      if(x.status==200){try{ult=JSON.parse(x.responseText);desenha(ult);$('off').style.display='none';}catch(e){}}
      else{$('off').style.display='block';}
      setTimeout(pede,1000);
    }
  };
  x.send();
}
window.onresize=ajusta;window.onorientationchange=ajusta;
ajusta();pede();
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/dados"):
            corpo = json.dumps(dados()).encode("utf-8")
            tipo = "application/json"
        else:
            corpo = HTML.encode("utf-8")
            tipo = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *a):
        pass

threading.Thread(target=coletor, daemon=True).start()
threading.Thread(target=thread_gpu, daemon=True).start()
threading.Thread(target=thread_fps, daemon=True).start()
print("Monitor rodando na porta %d  (Ctrl+C para parar)" % PORTA)
ThreadingHTTPServer(("0.0.0.0", PORTA), H).serve_forever()