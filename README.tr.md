<h1 align="center">subagent-verify</h1>
<p align="center"><b>Bir alt ajanın "değiştirdim" dediği dosyaların gerçekten değişip değişmediğini kontrol eden, değişmediyse ana ajanı uyaran Claude Code hook'u.</b></p>

<p align="center"><a href="README.md">English</a></p>

Bir alt ajan işini bitirip *"`src/app.py` dosyasını güncelledim, testleri ekledim, tüm testler geçti"* der. Ana ajan genelde buna inanır. subagent-verify önce git'e, dosya sistemine ve alt ajanın kendi araç çağrılarına bakar; sonucu, ana ajan sana "tamamlandı" demeden önce onun önüne koyar.

[intent-check](https://github.com/shadow-lo/intent-check)'in kardeşidir: intent-check *"yapılan iş istenenle uyuşuyor mu?"* diye sorar, subagent-verify *"alt ajan söylediğini gerçekten yaptı mı?"* diye sorar.

## ✨ Neyi yakalar

| Kod | Anlamı |
|---|---|
| `UNCHANGED` | Değiştirdim/oluşturdum diyor ama içerik, alt ajan çalışmadan önceki haliyle bayt bayt aynı |
| `MISSING` | Değiştirdim/oluşturdum diyor ama dosya yok |
| `NOT_DELETED` | Sildim diyor ama dosya duruyor |
| `TESTS_NOT_RUN` | Testler geçti diyor ama transkriptte hiç test komutu yok |
| `TESTS_FAILED` | Testler geçti diyor ama son test komutu hatayla bitmiş |
| `NO_REPORT` | quota-pilot işçisi `## WORKER REPORT` bloğuyla bitirmemiş |
| `PROTECTED` | issue-to-brief brief'inde *dokunulmamalı* denen bir yol değişmiş |
| `OUTSIDE` | İddia proje dışını gösteriyor (`../`, `/etc/...`, symlink kaçışı): reddedilir, dosya hiç açılmaz |

## 📦 Kurulum

`python3` ve `git` gerekir. Eklenti olarak (`.claude-plugin/plugin.json` + `hooks/hooks.json`) `/plugin` ile ekleyebilir ya da projeye klonlayıp [`examples/settings.json`](examples/settings.json) içeriğini `.claude/settings.json` dosyana ekleyebilirsin:

```bash
git clone https://github.com/shadow-lo/subagent-verify .claude/hooks/subagent-verify
```

## ⚙️ Nasıl çalışır

- **PreToolUse / SubagentStart:** Alt ajan başlamadan çalışma ağacının anlık görüntüsü alınır (HEAD + kirli/izlenmeyen her dosyanın SHA-256 parmak izi). İçerik değil, yalnızca yol ve parmak izi saklanır; paralel alt ajanlar `tool_use_id` / `agent_id` ile ayrılır.
- **PostToolUse:** Alt ajanın son mesajındaki iddialar çıkarılır ve yeni anlık görüntüyle karşılaştırılır (alt ajanın yaptığı commit'ler de sayılır). Hata varsa `decision: "block"` ile ana ajana gerekçe okutulur (hiçbir şey geri alınmaz); uyarılar `additionalContext` olarak gider. Her şey doğruysa sessiz kalır.
- **SubagentStop:** Anlık görüntü + alt ajan transkripti (hangi dosyaları araçla yazdı, hangi komutları çalıştırdı, başarısız oldular mı) kullanılır. Durma **bir kez** engellenir ve alt ajan işi yapar ya da raporunu düzeltir: katı ajanlarda her hatada, diğerlerinde yalnızca kesin kanıtta (`UNCHANGED`, `MISSING`, `TESTS_NOT_RUN` vb.). Arka planda çalışan alt ajanlar da böylece kapsanır; gerçek bir denemede yalan söyleyen alt ajan "`calc.py` dosyasını değiştirmedim, önceki iddiam yanlıştı" diye düzeltti.

İddialar iki kaynaktan gelir: yapılandırılmış `## WORKER REPORT` bloğu birebir ayrıştırılır; serbest metin ise temkinli okunur (yalnızca birinci tekil şahıs ya da madde işaretli ifadeler ve `Files changed:` listeleri). Türkçe ve İngilizce desteklenir.

## 🔗 quota-pilot ve issue-to-brief ile birlikte

- **quota-pilot:** `sonnet-worker` katı, `haiku-worker` ve Haiku modelde çalışan her alt ajan en katı seviyededir (en katı seviyede rapor eksikleri, kanıtsız test iddiaları ve bahsedilmeyen yazmalar da hata sayılır). `commands_run` içindeki her komutun transkriptte gerçekten çalıştırılmış olması gerekir. WORKER REPORT içindeki `files_changed` listesindeki her dosya gerçekten değişmiş olmalı, `tests: passed` için gerçek test çalıştırması gerekir. Her kontrol `.claude/subagent-verify/results.jsonl` dosyasına yalnızca meta veri içeren bir satır yazar; quota-pilot bunu okuyup sürekli hata yapan ucuz işçiyi bir üst seviyeye çıkarabilir.
- **issue-to-brief:** `.claude/briefs/current.json` varsa `must_not_touch` altındaki her değişiklik (git'in yok saydığı `.env` gibi dosyalar dahil) `PROTECTED` hatasıdır; brief alt ajan çalışmadan **önce** okunur, bu yüzden alt ajan brief'i silse ya da değiştirse de korumayı kaldıramaz (`CONTROL_CHANGED`); iş bitti denildiğinde dokunulmamış `likely` dosyalar ve kabul kriterleri ana ajana listelenir. Brief GitHub issue metninden geldiği için güvenilmez kabul edilir: asla çalıştırılmaz, yolları doğrulanır, metni kısaltılıp temizlenir.

## 🔒 Güvenlik

Shell yok, ağ yok, bağımlılık yok; `git status` hiç çalıştırılmaz (repo içine yerleştirilmiş bir filtre programı çalışamasın diye dosyalar `git ls-files` ile listelenip hook tarafından hash'lenir); sonuç dosyası symlink ya da hard link ile proje dışına yönlendirilemez; Python izole modda (`-I`) çalışır; git çağrıları sabit argümanlarla ve programı çalıştırabilecek repo ayarları kapatılarak yapılır; path traversal ve symlink kaçışı engellenir; gizli bilgiler `[REDACTED]` ile maskelenir; proje içindeki dosyalar kontrolü kapatamaz, yalnızca sıkılaştırabilir. Ayrıntılar: [SECURITY.md](SECURITY.md).

## ⚠️ Sınırlar

- Serbest metin sezgiseldir; alışılmadık ifadeler kaçabilir. WORKER REPORT hiç kaçmaz.
- Paralel alt ajanlar aynı dosyaları değiştirebilir; bu yüzden iddia edilmeyen değişiklikler hata değil not olarak gösterilir.
- Bir dosyanın değiştiğini kanıtlar, değişikliğin doğru olduğunu değil. Testlerle ya da intent-check ile birlikte kullan.

## 📄 Lisans

[MIT](LICENSE)
