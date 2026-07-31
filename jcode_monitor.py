#!/usr/bin/env python3
"""
JCODE MONITOR - botlari canli izleme paneli (macOS / terminal).

NE YAPAR:
  Railway'de calisan sistemin canli durumunu Mac'inde terminalde gosterir:
    * Sistem nabzi (kac canli mac, son veri ne zaman geldi, kac sinyal)
    * Taranan maclar ve dakikalari
    * Son sinyaller - ve o sinyalde 16 botun HER BIRININ ne dedigi
    * Bot bazli ozet

KULLANIM:
    pip3 install rich requests
    python3 jcode_monitor.py

  Ilk calistirmada e-posta/sifre sorar (uygulamada kayitli hesabin).
  Cikmak icin Ctrl+C.

NOT: Panel salt-okunurdur, hicbir sey degistirmez.
"""
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("Once 'rich' kutuphanesini kur:\n    pip3 install rich")
    sys.exit(1)

SUNUCU = os.environ.get("JCODE_URL", "https://web-production-f1dba.up.railway.app")
YENILEME = 10  # saniye

console = Console()
_cerez = {"v": None}


def istek(yol, veri=None):
    req = urllib.request.Request(
        SUNUCU + yol,
        data=json.dumps(veri).encode() if veri else None,
        headers={"Content-Type": "application/json",
                 **({"Cookie": _cerez["v"]} if _cerez["v"] else {})},
        method="POST" if veri else "GET",
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        ck = r.headers.get("Set-Cookie")
        if ck:
            _cerez["v"] = ck.split(";")[0]
        return json.loads(r.read().decode())


def giris():
    console.print(f"[bold cyan]JCODE MONITOR[/]  ->  {SUNUCU}\n")
    eposta = input("E-posta: ").strip()
    sifre = getpass.getpass("Sifre: ")
    try:
        d = istek("/api/login", {"email": eposta, "password": sifre})
    except Exception as e:
        console.print(f"[red]Sunucuya baglanilamadi: {e}[/]")
        sys.exit(1)
    if not d.get("success"):
        console.print(f"[red]Giris basarisiz: {d.get('error')}[/]")
        sys.exit(1)
    console.print("[green]Giris basarili.[/]\n")


def renk_karar(k):
    return {"goal": "bold green", "no_goal": "yellow",
            "insufficient_data": "dim"}.get(k, "white")


def ekran(d):
    n = d["nabiz"]
    ust = Table.grid(expand=True)
    ust.add_column(); ust.add_column(); ust.add_column(); ust.add_column(); ust.add_column()
    ust.add_row(
        f"[cyan]Canli mac[/]\n[bold]{n['canli_mac']}[/]",
        f"[cyan]Toplam sinyal[/]\n[bold]{n['toplam_sinyal']}[/]",
        f"[cyan]Acik sinyal[/]\n[bold]{n['acik_sinyal']}[/]",
        f"[cyan]Takim profili[/]\n[bold]{n['takim_profili']}[/]",
        f"[cyan]Son veri[/]\n[bold]{(n['son_veri'] or '-')[-8:]}[/]",
    )
    parcalar = [Panel(ust, title="SISTEM NABZI", border_style="cyan")]

    # Taranan maclar
    if d["maclar"]:
        t = Table(expand=True, show_edge=False)
        for c, j in [("Dk", "right"), ("Mac", "left"), ("Skor", "center"),
                     ("Durum", "left"), ("Lig", "left"), ("Veri", "right")]:
            t.add_column(c, justify=j, no_wrap=(c != "Mac"))
        for m in d["maclar"][:12]:
            t.add_row(f"{m['dakika']}'", f"{m['ev']} - {m['dep']}", m["skor"],
                      m["durum"], (m["lig"] or "")[:22], str(m["snapshot"]))
        parcalar.append(Panel(t, title=f"TARANAN MACLAR ({len(d['maclar'])})",
                              border_style="blue"))
    else:
        parcalar.append(Panel("[dim]Su an canli mac yok.[/]",
                              title="TARANAN MACLAR", border_style="blue"))

    # Son sinyal + bot oylari
    if d["sinyaller"]:
        s = d["sinyaller"][0]
        bas = (f"[bold]{s['mac']}[/]  {s['dakika']}'  |  {s['market']}  |  "
               f"olasilik [bold]%{100*s['olasilik']:.0f}[/]  |  {s['seviye']}  |  "
               f"sonuc [bold]{s['sonuc']}[/]")
        bt = Table(expand=True, show_edge=False)
        bt.add_column("Bot", no_wrap=True)
        bt.add_column("Karar", no_wrap=True)
        bt.add_column("Olasilik", justify="right", no_wrap=True)
        for o in s["oylar"]:
            bt.add_row(o["bot"],
                       Text(o["karar"], style=renk_karar(o["karar"])),
                       f"{o['olasilik']:.2f}" if o["olasilik"] else "-")
        if not s["oylar"]:
            bt.add_row("[dim]bot kaydi yok (eski sinyal)[/]", "", "")
        parcalar.append(Panel(Group(Text.from_markup(bas), bt),
                              title="SON SINYAL - BOT OYLARI", border_style="magenta"))

        gt = Table(expand=True, show_edge=False)
        for c in ["Mac", "Dk", "Market", "Olasilik", "Seviye", "Sonuc"]:
            gt.add_column(c, no_wrap=True)
        for x in d["sinyaller"]:
            renk = {"WON": "green", "LOST": "red"}.get(x["sonuc"], "yellow")
            gt.add_row(x["mac"][:34], f"{x['dakika']}'", x["market"] or "-",
                       f"%{100*x['olasilik']:.0f}", x["seviye"],
                       Text(x["sonuc"], style=renk))
        parcalar.append(Panel(gt, title="SON SINYALLER", border_style="green"))
    else:
        parcalar.append(Panel("[dim]Henuz sinyal uretilmedi.[/]",
                              title="SINYALLER", border_style="magenta"))

    return Group(*parcalar)


def main():
    giris()
    with Live(console=console, refresh_per_second=4, screen=True) as live:
        while True:
            try:
                d = istek("/api/monitor")
                if not d.get("success"):
                    live.update(Panel(f"[red]{d.get('error')}[/]"))
                else:
                    live.update(ekran(d))
            except Exception as e:
                live.update(Panel(f"[red]Baglanti hatasi: {e}[/]\n[dim]Yeniden denenecek...[/]"))
            time.sleep(YENILEME)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Panel kapatildi.[/]")
