import os
from html import escape
from datetime import datetime
import streamlit as st
from mortis_control import ACTIVE_STATUSES, read_status, write_status
from mortis_core import CHAT_DEFAULT, EMBED_DEFAULT, ask, cleanurl, conn, crawl, models

st.set_page_config(page_title="AI Web Spider",page_icon="🕷️",layout="wide")
st.markdown("""<style>
.stApp{background:radial-gradient(ellipse at top left,#172943,#0a101b 70%);color:#e9f2ff}
.block-container{max-width:1450px;padding-top:1.4rem}
.hero{padding:22px 26px;border:1px solid #2b486a;border-radius:20px;background:linear-gradient(120deg,#172c48,#101827)}
.spider{font-size:56px;filter:drop-shadow(0 0 14px #42b8ff);animation:bob 2s ease-in-out infinite}
@keyframes bob{50%{transform:translateY(-7px) rotate(4deg)}}
div[data-testid="stMetric"]{background:#132137;border:1px solid #2a405e;border-radius:12px;padding:10px}
.source{font-size:.85rem;color:#8ecbff;overflow-wrap:anywhere}
</style><div class="hero"><div style="display:flex;gap:20px;align-items:center"><div class="spider">🕷️</div><div><h1>AI WEB SPIDER</h1><p>A local website crawler that builds a searchable knowledge base and answers questions with source citations.</p></div></div></div>""",unsafe_allow_html=True)
with st.sidebar:
 st.header("⚙️ Spider settings")
 chatmodel=st.text_input("Ollama chat model",CHAT_DEFAULT)
 embedmodel=st.text_input("Ollama embedding model",EMBED_DEFAULT)
 ms=models()
 if ms: st.success("Ollama connected"); st.caption("Installed: "+", ".join(ms[:8]))
 else:
  st.warning("Ollama not detected")
  st.code("ollama pull llama3.2\nollama pull nomic-embed-text",language="powershell")
  st.markdown("[Install Ollama](https://ollama.com/download)")
 if read_status(os.environ.get("MORTIS_STATUS_FILE")) not in ACTIVE_STATUSES:
  write_status(os.environ.get("MORTIS_STATUS_FILE"),"ready")
 st.caption("Same host only · robots.txt checked · 2 MB/page · 15-second timeout")
 if os.environ.get("MORTIS_STOP_FILE"):
  st.caption("Double-click the desktop spider to request a graceful crawl stop.")
left,right=st.columns([1.05,.95],gap="large")
with left:
 st.subheader("🕸️ Crawl a website")
 start=st.text_input("Starting URL","https://docs.python.org/3/")
 a,b=st.columns(2)
 with a: limit=st.slider("Maximum pages",1,100,8)
 with b: delay=st.slider("Delay between pages (seconds)",0.5,5.0,1.5,0.5)
 run=st.button("🕷️ Start crawl and index",type="primary",use_container_width=True)
 bar=st.progress(0); status=st.empty(); logbox=st.empty()
 if run:
  if not ms: st.error("Install/start Ollama and pull both models first.")
  else:
   logs=[]
   def log(msg):
    logs.append(datetime.now().strftime("%H:%M:%S")+"  "+msg); logbox.code("\n".join(logs[-18:]))
   def prog(done,total,n):
    bar.progress(min(1,done/max(1,total))); status.info(f"Pages: {done}/{total} · Indexed chunks: {n}")
   try:
    with st.spinner("Spider is crawling, extracting text and indexing..."):
     done,n=crawl(start,limit,delay,embedmodel,log,prog)
    st.success(f"Finished: {done} pages, {n} chunks."); st.rerun()
   except Exception as e: st.error(str(e))
   finally:
    if (read_status(os.environ.get("MORTIS_STATUS_FILE")) != "stopped"
        and read_status(os.environ.get("MORTIS_STATUS_FILE")) not in ACTIVE_STATUSES):
     write_status(os.environ.get("MORTIS_STATUS_FILE"),"ready")
with right:
 st.subheader("🧠 Ask your spider")
 question=st.text_area("Question","",placeholder="What are the main concepts explained by this website?",height=110)
 if st.button("Ask AI Spider",use_container_width=True):
  if not ms: st.error("Ollama is not running.")
  elif not question.strip(): st.warning("Enter a question.")
  else:
   try:
    with st.spinner("Retrieving evidence and generating answer..."): answer,found=ask(question,chatmodel,embedmodel)
    st.markdown("#### Answer"); st.markdown(answer)
    if found:
     st.markdown("#### Retrieved sources")
     for i,(score,url,title,body) in enumerate(found,1):
      with st.expander(f"[Source {i}] relevance {score:.3f}"):
       st.write(title)
       st.markdown(f'<div class="source">{escape(url, quote=True)}</div>',unsafe_allow_html=True)
       st.write(body)
       safe_url=cleanurl(url)
       if safe_url: st.link_button("Open original page",safe_url)
   except Exception as e: st.error(f"AI request failed: {e}")
c=conn()
pc=c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
cc=c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
rows=c.execute("SELECT url,title,created,status,length(body) FROM pages ORDER BY created DESC").fetchall()
c.close()
st.divider()
x,y,z=st.columns(3); x.metric("Pages collected",pc); y.metric("Searchable chunks",cc); z.metric("AI engine","Ollama local" if ms else "Not connected")
with st.expander("📚 Browse collected pages"):
 if not rows: st.info("No pages yet. Start your first crawl.")
 for u,t,d,s,n in rows:
  source_status="Browser capture" if s==0 else f"HTTP {s}"
  st.write(t)
  st.markdown(f'<div class="source">{escape(u, quote=True)}</div>',unsafe_allow_html=True)
  st.caption(f"{source_status} · {n:,} characters · {d}")
  st.divider()
with st.expander("🧹 Manage local knowledge base"):
 st.caption("Deletes the local database stored beside this app.")
 if st.button("Delete all collected data"):
  c=conn(); c.execute("DELETE FROM pages"); c.execute("DELETE FROM chunks"); c.commit(); c.close(); st.rerun()
