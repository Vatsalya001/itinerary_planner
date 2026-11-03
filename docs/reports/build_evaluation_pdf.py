from pathlib import Path
import re, json, hashlib, subprocess
from datetime import datetime, UTC
from xml.sax.saxutils import escape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
import reportlab
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
font_dir = Path(reportlab.__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("Body", str(font_dir / "Vera.ttf")))
pdfmetrics.registerFont(TTFont("Body-Bold", str(font_dir / "VeraBd.ttf")))
pdfmetrics.registerFontFamily("Body",normal="Body",bold="Body-Bold",italic="Body",boldItalic="Body-Bold")

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT / 'output/pdf/EVALUATION_REPORT.pdf'
OUT.parent.mkdir(parents=True, exist_ok=True)
BASE='https://github.com/Vatsalya001/itinerary_planner/blob/main/'
head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
navy=colors.HexColor('#142B40');teal=colors.HexColor('#007D7A');muted=colors.HexColor('#516171');pale=colors.HexColor('#EEF5F7');line=colors.HexColor('#D9E3E8')
styles={
 'title':ParagraphStyle('title',fontName='Body-Bold',fontSize=29,leading=33,textColor=navy,spaceAfter=14),
 'h':ParagraphStyle('h',fontName='Body-Bold',fontSize=18,leading=22,textColor=navy,spaceAfter=12),
 'sub':ParagraphStyle('sub',fontName='Body-Bold',fontSize=11,leading=15,textColor=teal,spaceBefore=13,spaceAfter=6),
 'body':ParagraphStyle('body',fontName='Body',fontSize=9.5,leading=13.4,textColor=navy,spaceAfter=9),
 'small':ParagraphStyle('small',fontName='Body',fontSize=8.2,leading=11,textColor=muted,spaceAfter=6),
 'cell':ParagraphStyle('cell',fontName='Body',fontSize=9,leading=12,textColor=navy),
 'th':ParagraphStyle('th',fontName='Body-Bold',fontSize=8.7,leading=11,textColor=colors.white),
 'code':ParagraphStyle('code',fontName='Courier',fontSize=8,leading=12,textColor=navy,spaceAfter=8),
 'label':ParagraphStyle('label',fontName='Body-Bold',fontSize=9,leading=12,textColor=teal,spaceAfter=12),
}
def p(text,style='body'):return Paragraph(text,styles[style])
def link(path,label):return '<link href="'+(BASE.replace('/blob/','/tree/') if path.endswith('/') else BASE)+path+'" color="#007D7A">'+escape(label)+'</link>'
def table(headers,rows,widths):
 data=[[p(escape(str(c)),'th') for c in headers]]+[[p(str(c),'cell') for c in row] for row in rows]
 t=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
 t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),navy),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,pale]),('LINEBELOW',(0,-1),(-1,-1),.5,line)]))
 return t
story=[]
def add(text,style='body'):story.append(p(text,style))
def page(n,title):
 if story:story.append(PageBreak())
 add(f'PERFECT SATURDAY PLANNER  /  {n:02d}','label');add(title,'h')
def bullet(text):add('• '+text)
def metric_rows(path):
 text=(ROOT/path).read_text();part=text.split('## Model leaderboard',1)[1].split('Rates show',1)[0]
 rows=[]
 for s in part.splitlines():
  if s.startswith('| `'):
   cells=[x.strip().replace('`','') for x in s.strip('|').split('|')];rows.append(cells)
 return rows
hist=metric_rows('docs/EVAL_REPORT.md');release=metric_rows('docs/verification/RELEASE_EVAL_REPORT.md')
assert len(hist)==6 and len(release)==2
# Verify selected historical session pass counts directly against the committed raw records.
prov=(ROOT/'docs/EVAL_REPORT.md').read_text().split('## Provenance',1)[1]
provenance=[]
for s in prov.splitlines():
 if s.startswith('| `'):
  c=[x.strip().replace('`','') for x in s.strip('|').split('|')]
  if len(c)==11:provenance.append(c)
for row in hist:
 pr=next(c for c in provenance if c[0]==row[0] and c[1]=='models')
 raw=[json.loads(s) for s in (ROOT/'backend/evals/results'/pr[-1]).read_text().splitlines() if s.strip()]
 selected=[x for x in raw if x.get('session')==pr[2] and x.get('suite')=='models']
 assert len(selected)==int(row[2].split('/')[0])
 assert sum(bool(x['hard_pass']) for x in selected)==int(re.search(r'\((\d+)/',row[3])[1])

page(1,'Evaluation report')
add('Evidence-led assessment of the agent, deterministic guardrails and product workflow.','title')
add('Prepared 3 November 2026 • Vatsalya Roy','small')
add('Code assessed: <b>'+head[:12]+'</b>. This PDF consolidates committed reports and raw result files. It does not run new model evaluations or claim that historical model scores describe the current deployment.','body')
story.append(table(['Current code checks','Recorded outcome'],[
 ['Backend regression suite','1,881 passed; 11 live/Ollama cases deselected'],['Frontend unit/component suite','146 passed; lint, types and build passed'],['Chromium product checks','21 passed with production bundle and fixture API'],['Offline workflow/fault eval','32/32 hard passes; scripted fake model'],['Static and integration checks','Ruff; mypy across 75 source files; Postgres and Docker smoke CI passed']], [190,321]))
add('What the evidence supports','sub')
add('The application has extensive automated checks for deterministic correctness, orchestration and failure handling. Historical experiments also measure hosted and local model behaviour in a frozen synthetic world. These are distinct evidence layers: passing application tests does not establish real-world recommendation quality or venue accessibility.')
add('Three evidence layers','sub')
for t in ['<b>Current code verification:</b> timing policy, safe final-stop fitting, constraint checks, API/UI/storage and deployment smoke checks. [S1, S2]', '<b>Historical model evaluations:</b> six model/baseline rows, one selected repeat per case, with failures retained. [S3, S5]', '<b>Live observations:</b> small production smoke samples and user-reported examples; useful for finding defects, not a reliability benchmark. [S6, S7]']:bullet(t)
add('Deployment: Cerebras GPT-OSS 120B, FastAPI, LangGraph and React. Source: '+link('README.md','repository README')+'. Current CI: <link href="https://github.com/Vatsalya001/itinerary_planner/actions/runs/41684470647" color="#007D7A">release verification</link>.','small')

page(2,'Historical model comparison')
add('Frozen-world results • report generated 2 November 2026, 22:33 UTC','small')
add('Each complete row covers 26 model cases: 18 development and 8 holdout cases. Groq covers only 5/26. The report selected the latest complete session per suite/model, or its latest partial session; 705 stored records include other historical sessions, not 705 independent cases in this table. [S3]')
labels={'baseline:deterministic':'Deterministic baseline','cerebras:gpt-oss-120b':'Cerebras GPT-OSS 120B','groq:openai/gpt-oss-120b':'Groq GPT-OSS 120B','ollama:qwen3.5:9b':'Ollama Qwen3.5 9B','ollama:gemma4:e4b':'Ollama Gemma4 E4B','ollama:granite4:tiny-h':'Ollama Granite4 Tiny-H'}
for title,rows in [('Baseline and hosted APIs',hist[:3]),('Local Ollama models',hist[3:])]:
 add(title,'sub');story.append(table(['Model','Cases','Hard pass','Status accuracy','p50','p95'],[[labels[r[0]],r[2].replace(' (partial)','*'),r[3],r[4],r[14],r[15]] for r in rows],[145,49,91,91,67,68]))
add('* Groq is a partial sample. Local runs had a 240-second deadline versus 60 seconds for hosted runs. Hardware was not normalized; compare quality and latency within this context, not as a controlled hardware benchmark. No judge model was used. One repeat per case provides no repeatability estimate.','small')
add('Observed failure patterns','sub')
story.append(table(['Population','Recorded errors / degradations (runs affected)'],[
 ['Cerebras','No submission: 1; turn cap: 5'],['Groq','Model timeout: 2; model unavailable: 2 (categories can overlap)'],['Qwen3.5 9B','Turn cap: 1; timeout: 2; parser degradation: 1; exhausted repairs: 3'],['Gemma4 E4B','No submission: 6; parser degradation: 1; exhausted repairs: 1'],['Granite4 Tiny-H','Exhausted repairs: 18']], [145,366]))
add('Interpretation: Cerebras led these historical hosted samples; Qwen led the local hard-pass rows. Neither outcome is a current production-quality guarantee. Zero registry-priced local cost excludes hardware/electricity, and recorded API cost is not a billing invoice.','small')

page(3,'Methodology and guardrail coverage')
add('How a run is scored','sub')
add('<b>Hard pass:</b> every applicable code assertion passes, including the expected status. A fallback can be useful to a user yet fail a model case that requires agent completion. Harness exceptions count as failures. <b>Status accuracy:</b> returned status is in the expected set. <b>Agent success:</b> agent completion on feasible cases. Percentages must be read with their denominators. [S3, S4]')
story.append(table(['Area','What is checked'],[
 ['Grounding and evidence','Selected IDs must come from tools in the same run; unknown dietary/access fields cannot satisfy hard constraints.'],['Budget and timing','Independent assertions recompute costs, contingency, transfers, buffers and schedule feasibility from fixture data.'],['Opening hours and weather','Hours, daylight, indoor/rain alternatives and event timing are checked against frozen inputs.'],['Preference parsing','Explicit fields, constraint preservation, clarification precision/recall and expected parse fields.'],['Failures and product flow','Provider faults, refusal, timeouts, repairs, clarification resume, SSE, share links and storage.'],['Security','Injection and XSS scenarios, output/trace secret checks, input limits and rate-limit handling.']], [127,384]))
add('Current window policy and boundary tests','sub')
add('Target 85% of the available time, with an 80-100% practical range. A four-hour window targets 204 minutes; its lower margin is 192 minutes. Travel and buffers count. A final flexible venue may lose at most 15 minutes, never below its minimum visit length; fixed events remain intact. Every adjusted draft is revalidated. [S1]')
add('New synthetic tests cover 1/10/15-minute clipping, excessive overrun, insufficient visit duration, fixed events, budget preservation, deterministic final-venue fitting, 85% filling, the exact 80% UI boundary, and medium energy when no mood is given. Explicit tiredness remains low energy.')
add('Limits of the methodology','sub')
add('Fixtures are synthetic attributes on real or OSM-derived names, with fixed weather, events, travel and clock. Some explanation assertions are keyword proxies. Browser/judgment assertions do not count as model hard assertions. Only one repeat per model case is selected; no LLM judge or statistically meaningful production latency study was performed. Local and hosted deadlines differ. [S3, S4]','small')

page(4,'Release checks and live observations')
add('Later saved release evaluation • 3 November 2026, 09:14 UTC','sub')
story.append(table(['Population','Hard pass','Scope'],[
 ['Deterministic baseline','26/26 (100%)','Complete frozen model suite'],['Cerebras GPT-OSS 120B','2/3 (66.7%)','Partial suite; two runs recorded provider rate limiting'],['Scripted fake agent','32/32 (100%)','Workflow/fault suite, not model intelligence']], [166,110,235]))
add('This later report has 61 selected records. It is separate from the historical model comparison and predates the current window policy. It contains no new Ollama comparison. Its provenance has unknown Git SHA fields, so the report cannot establish exact current-code model performance. [S4]','small')
add('Pre-policy production smoke samples','sub')
story.append(table(['City / run ID','Outcome','Coverage / estimate'],[
 ['Mumbai / r_7ea0e55eff6e','Agent; 6,989 ms','175/240 min; INR 1,929; museums uncovered'],['Delhi / r_19f3cd88cf1e','Agent; 18,298 ms','90/120 min; INR 660; history uncovered'],['Gurugram / r_e0aee77953c4','Labelled fallback; 13,475 ms','110/240 min; INR 880; partial art/museum coverage']], [168,137,206]))
add('These single observations on release 48a78a2 are recorded in S6. They are not new-policy acceptance tests, and model-call limits still apply. The current application CI used no hosted inference. Do not merge smoke samples into the frozen-world leaderboard.','small')
add('Post-policy user-reported examples and open review items','sub')
add('The user reported Mysuru at 204/240 minutes (85%), Bundi at 180/180 (100%) and Gurugram at 240/240 (100%). These are manually supplied observations, not new raw eval records or independently audited venues. They are excluded from scored rates.')
add('Two review items remain: a pasted Bundi timeline showed “0:00 AM” while its share text showed 10:00 AM (not independently reproduced); a wheelchair request received unverified auto-rickshaw transfers and a venue with first-floor address information. Entrance-only evidence does not prove lift, internal-facility or vehicle accessibility. These require product review and real-world confirmation, not a claim of complete accessibility.')
add('Generic admission estimates keep budget checks provisional. Sparse data may still produce a labelled partial plan. Free hosting can sleep, and provider throttling/deadlines can cause labelled backup planning. No zero-defect or universal 80% coverage guarantee is made.','small')

page(5,'Evidence, provenance and reproduction')
add('Source index (all links point to committed repository evidence)','sub')
refs=[('S1','docs/verification/WINDOW_POLICY_2026-11-03.md','Current window policy and regression results'),('S2','README.md','Architecture, setup and current verification summary'),('S3','docs/EVAL_REPORT.md','Historical 705-record report and model comparison'),('S4','docs/verification/RELEASE_EVAL_REPORT.md','Later 61-record release report'),('S5','backend/evals/results/2026-11-02/','Historical JSONL files, including Ollama'),('S6','docs/verification/PLAN_QUALITY_2026-11-03.md','Pre-policy live run evidence and limitations'),('S7','docs/verification/PRACTICAL_GUARDRAILS_2026-11-03.md','Admission, transport and accessibility evidence'),('S8','eval_cases.json','Evaluation case specification')]
for code,path,label in refs:add('<b>'+code+'</b>  '+link(path,label),'small')
add('Selected historical sessions','sub')
story.append(table(['Population','Session','Recorded code SHA'],[[labels.get(c[0],c[0]),c[2],c[4]] for c in provenance if c[1]=='models'],[155,238,118]))
add('The generator cross-checks every historical table row against its selected committed JSONL session: model case count and hard-pass numerator. Current code snapshot: '+head+'. Historical raw results retain original hashes and older sessions; no record was rewritten for this PDF.','small')
add('Reproduce locally (from backend/)','sub')
for cmd in ['uv sync --frozen','uv run pytest','uv run ruff check app tests','uv run mypy app','uv run python -m evals.runner --suite offline --out /tmp/saturday-evals','uv run python -m evals.runner --suite models --models ollama:qwen3.5:9b --repeats 3 --out /tmp/saturday-local-evals']:
 add(escape(cmd),'code')
add('Ollama must be installed, running and have the named model available. Hosted-model reruns require provider keys and quota. Keep results in separate output folders; do not overwrite historical evidence. Frontend commands: npm ci, npm run lint, npm test, npm run build, npm run e2e. See the README for environment setup.','small')

def footer(canvas,doc):
 canvas.saveState();canvas.setStrokeColor(line);canvas.line(42,43,A4[0]-42,43);canvas.setFillColor(muted);canvas.setFont('Body',8);canvas.drawString(42,29,'Perfect Saturday Planner | Evaluation evidence | 3 Nov 2026');canvas.drawRightString(A4[0]-42,29,f'{doc.page}');canvas.restoreState()
doc=SimpleDocTemplate(str(OUT),pagesize=A4,rightMargin=42,leftMargin=42,topMargin=36,bottomMargin=52,title='Perfect Saturday Planner - Evaluation Report',author='Vatsalya Roy',subject='Historical hosted and local model evaluations, current regression checks, methodology and limitations')
doc.build(story,onFirstPage=footer,onLaterPages=footer)
print(json.dumps({'pdf':str(OUT),'bytes':OUT.stat().st_size,'source_commit':head,'historical_rows_verified':len(hist)}))
