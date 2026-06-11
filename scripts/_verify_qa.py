import sys, datetime
sys.path.insert(0, "scripts")
import run, feedparser
feed = feedparser.parse("https://rss.libsyn.com/shows/254861/destinations/1928300.xml")
ent = [(datetime.datetime(*e.published_parsed[:6]) + datetime.timedelta(hours=3), e) for e in feed.entries]
ent = [x for x in ent if x[0].strftime("%Y-%m-%d") == "2026-06-10"]
ent.sort(key=lambda x: x[0])
pub, entry = ent[1]
ep = {"id": "x", "podcast": "All-In", "title": entry.title, "pub_dt": pub, "date": "2026-06-10"}
vid = run.find_youtube_id(ep["title"], ep["podcast"])
tr = run.get_transcript(vid)
content = run.generate_content(ep, tr, vid, model=run.MODEL)
review = run.qa_content_review(ep, content, tr)
print("REVIEW_RESULT_TYPE:", type(review).__name__)
print("REVIEW:", review)
