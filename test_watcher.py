import unittest
from datetime import date, datetime, timezone

import watcher as w

NOW = datetime(2026, 11, 23, 4, 0, tzinfo=timezone.utc)  # 10:00 Dhaka
PAGE = "https://example.edu.bd/career"


def page(*rows):
    body = "".join(f'<tr><td><a href="/job/{i}">{title}</a></td><td>Deadline: {deadline}</td></tr>'
                   for i, (title, deadline) in enumerate(rows))
    return f"<html><body><nav><a href='/x'>Faculty Members</a></nav><table>{body}</table></body></html>"


class FakeFetcher:
    def __init__(self, html):
        self.html = html

    def __call__(self, sources):
        if self.html is None:
            return {s["url"]: w.FetchResult(s["url"], False, "timeout") for s in sources}
        return {s["url"]: w.FetchResult(s["url"], True, "ok", self.html, s["url"]) for s in sources}


class FailingNotifier:
    def send(self, text):
        raise w.TelegramError("down")


CONFIG = {"universities": [{"name": "Example University", "monitor_urls": [PAGE], "urls": [PAGE], "status": "Ready"},
                           {"name": "Blocked University", "monitor_urls": [], "urls": ["https://blocked.edu.bd/jobs"],
                            "status": "Blocked"},
                           {"name": "Bad Site", "monitor_urls": [], "urls": ["https://bad.example/"],
                            "status": "Suspicious site"}], "backup_sources": []}


class Dates(unittest.TestCase):
    def test_formats(self):
        for text in ["Deadline: 25/11/2026", "Dead Line: 25-11-2026", "Application deadline : 2026-11-25",
                     "Last date: 25 Nov 2026", "apply by November 25, 2026", "শেষ তারিখ: ২৫/১১/২০২৬",
                     "আবেদনের শেষ তারিখ ২৫ নভেম্বর ২০২৬", "২৫/১১/২০২৬ তারিখের মধ্যে আবেদন", "on or before 25th November, 2026"]:
            self.assertEqual(w.find_deadline(text), date(2026, 11, 25), text)

    def test_posted_date_is_not_deadline(self):
        self.assertEqual(w.find_deadline("Posting Time : 19/11/2026 Application Deadline : 28/11/2026"), date(2026, 11, 28))
        self.assertIsNone(w.find_deadline("Published 19/11/2026"))


class Classification(unittest.TestCase):
    def verdict(self, title, context="", url=PAGE):
        return w.classify(title, context or title, url)

    def test_keeps_wanted_posts(self):
        for title in ["Lecturer, Department of English", "Research Assistant (2 posts)", "Assistant Registrar",
                      "Faculty Search: Spring 2027", "প্রভাষক পদে নিয়োগ বিজ্ঞপ্তি", "Junior Administrative Officer",
                      "Office of the Controller of Examinations - Officer"]:
            self.assertIsNotNone(self.verdict(title), title)

    def test_rejects_noise(self):
        for title in ["Career Fair 2026", "Admission Notice Spring 2027", "Faculty Members", "Junior Electrician needed",
                      "Written exam result for Lecturer", "Holiday notice", "Tender notice"]:
            self.assertIsNone(self.verdict(title, url="https://example.edu.bd/notice"), title)

    def test_school_post_goes_to_digest(self):
        self.assertEqual(self.verdict("Lecturer (English)", "Lecturer (English) JU School and College")[0], "possible")


class Extraction(unittest.TestCase):
    def test_generic_link_uses_card_title(self):
        html = ("<main><div class='card'><h3>School of Law</h3><p>Faculty Search</p><p>Application Deadline : 29/11/2026</p>"
                "<a href='/apply/1'>Apply Link</a><a href='/detail/1'>Detail information</a></div></main>")
        item = w.extract_items("IUB", html, PAGE)[0]
        self.assertIn("Faculty Search", item.title)
        self.assertTrue(item.url.endswith("/detail/1"))
        self.assertEqual(item.deadline, date(2026, 11, 29))

    def test_card_without_links(self):
        html = "<main><div><div>কর্মকর্তা (অক্টোবর ২০২৬) Published on : 2026-10-28 Application deadline : 2026-11-25</div></div></main>"
        self.assertEqual(w.extract_items("BUET", html, PAGE)[0].deadline, date(2026, 11, 25))

    def test_session_parameter_does_not_change_key(self):
        a = w.Item("U", "Lecturer", "https://a.edu/job?id=1&gsessionid=abc", PAGE, "")
        b = w.Item("U", "Lecturer", "https://a.edu/job?id=1&gsessionid=xyz", PAGE, "")
        self.assertEqual(w.item_key(a), w.item_key(b))


    def test_heading_and_details_button_are_one_circular(self):
        html = ("<main><div><h3><a href='/job/7'>Lecturer, Department of English (02 post)</a></h3></div>"
                "<div><p>Teacher - Grade 9</p><p>Deadline: 29/11/2026</p><a href='/job/7'>View Details</a></div></main>")
        items = w.extract_items("DU", html, PAGE)
        self.assertEqual(len(items), 1)
        self.assertIn("Department of English", items[0].title)
        self.assertEqual(items[0].deadline, date(2026, 11, 29))

    def test_two_bare_lecturer_posts_stay_separate(self):
        html = ("<main><article><a href='/career/lecturer-law-2026'>Lecturer</a><p>Apply by 15 December 2026</p></article>"
                "<article><a href='/career/lecturer-english-2026'>Lecturer</a><p>Apply by 15 December 2026</p></article></main>")
        items = [i for i in w.extract_items("SU", html, PAGE) if w.classify(i.title, i.context, PAGE)]
        self.assertEqual(len({w.item_key(i) for i in items}), 2)
        self.assertIn("law", items[0].title)

    def test_card_with_only_deadline_and_generic_link(self):
        html = ("<main><h3>Assistant Registrar</h3><div><p>Application Deadline: 26/11/2026</p>"
                "<a href='/ad.pdf'>view job advertisement</a></div></main>")
        item = w.extract_items("BUFT", html, PAGE)[0]
        self.assertEqual(item.title, "Assistant Registrar")
        self.assertIsNotNone(w.classify(item.title, item.context, PAGE))

    def test_circular_pdf_in_top_menu_is_found(self):
        html = ("<nav><a href='/front/images/circular_11_11_2026.pdf'>Download Job Circular</a><a href='/x'>Education</a></nav>"
                "<main><select><option>Lecturer</option><option>Section Officer</option></select></main>")
        items = [i for i in w.extract_items("UITS", html, "https://career.uits.ac.bd/")
                 if w.classify(i.title, i.context, i.page_url)]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Job Circular (11 Nov 2026)")
        self.assertTrue(items[0].url.endswith("circular_11_11_2026.pdf"))

    def test_form_dropdown_is_not_a_job_line(self):
        self.assertEqual(w.job_lines(w.page_lines("<main><select><option>Section Officer</option></select></main>")), [])

    def test_table_deadline_column_and_shared_form(self):
        html = ("<table><tr><th>Discipline</th><th>Apply for</th><th>Deadline</th></tr>"
                "<tr><td>Admin</td><td><a href='/form'>Senior Officer, Admin</a></td><td>31 October 2026</td></tr>"
                "<tr><td>IT</td><td><a href='/form'>IT Officer</a></td><td>05 November 2026</td></tr></table>")
        items = w.extract_items("Green", html, PAGE)
        self.assertEqual({i.title: i.deadline for i in items},
                         {"Senior Officer, Admin": date(2026, 10, 31), "IT Officer": date(2026, 11, 5)})

    def test_two_digit_year(self):
        self.assertEqual(w.find_dates("Date: 27-Oct-26")[0][1], date(2026, 10, 27))

    def test_subject_name_is_not_admin_post(self):
        for title in ["Department of Business Administration", "Master of Business Administration", "Faculty & Staff"]:
            self.assertIsNone(w.classify(title, title, PAGE), title)
        self.assertIsNotNone(w.classify("Lecturer (Business Administration)", "Lecturer (Business Administration)", PAGE))

    def test_gambling_spam_is_rejected(self):
        title = "Pemodelan Matematis Putaran Gratis Buatan Developer PG Soft"
        self.assertIsNone(w.classify(title, title, PAGE))

    def test_real_circular_with_application_form_is_kept(self):
        title = "সহকারী অধ্যাপক পদে নিয়োগ বিজ্ঞপ্তি ও আবেদন ফরম"
        self.assertIsNotNone(w.classify(title, title, PAGE))
        self.assertIsNone(w.classify("Application Form for Teacher", "Application Form for Teacher", PAGE))

    def test_menu_word_without_deadline_is_ignored(self):
        self.assertIsNone(w.classify("Lecturer", "Lecturer", PAGE))

    def test_date_is_removed_from_title(self):
        self.assertEqual(w.tidy_title("Faculty Search [Posted: November 04, 2026] [Deadline: November 30, 2026]"), "Faculty Search")

    def test_clerical_post_in_registrar_office_is_skipped(self):
        self.assertIsNone(w.classify("উচ্চমান সহকারী, Office of the Registrar (01 post)", "Deadline: 15/12/2026", PAGE))


class Flow(unittest.TestCase):
    def go(self, state, html, notifier=None, now=NOW):
        notifier = notifier or w.DryRunSilent()
        w.run(CONFIG, state, FakeFetcher(html), notifier, now)
        return notifier.messages

    def setUp(self):
        self.state = w.new_state()
        self.state["last_digest"] = "2026-11-23"

    def test_first_run_lists_open_only_once(self):
        html = page(("Lecturer, CSE", "30/11/2026"), ("Lecturer, EEE", "01/01/2025"))
        first = self.go(self.state, html)
        self.assertEqual(len(first), 1)
        self.assertIn("CSE", first[0])
        self.assertNotIn("EEE", first[0])
        self.assertEqual(self.go(self.state, html), [])

    def test_new_circular_alerts_once(self):
        self.go(self.state, page(("Lecturer, CSE", "30/11/2026")))
        html = page(("Lecturer, CSE", "30/11/2026"), ("Research Assistant, Physics", "05/12/2026"))
        messages = self.go(self.state, html)
        self.assertEqual(len(messages), 1)
        self.assertIn("Research Assistant", messages[0])
        self.assertEqual(self.go(self.state, html), [])

    def test_expired_new_item_is_silent(self):
        self.go(self.state, page(("Lecturer, CSE", "30/11/2026")))
        self.assertEqual(self.go(self.state, page(("Lecturer, CSE", "30/11/2026"), ("Lecturer, Old", "01/10/2026"))), [])

    def test_failed_telegram_retries_next_run(self):
        self.go(self.state, page(("Lecturer, CSE", "30/11/2026")))
        html = page(("Lecturer, CSE", "30/11/2026"), ("Assistant Registrar", "05/12/2026"))
        with self.assertRaises(w.TelegramError):
            w.run(CONFIG, self.state, FakeFetcher(html), FailingNotifier(), NOW)
        self.assertIn("Assistant Registrar", "".join(self.go(self.state, html)))

    def test_redesign_flood_is_grouped(self):
        self.go(self.state, page(("Lecturer, CSE", "30/11/2026")))
        rows = [(f"Lecturer Post {i}", "30/12/2026") for i in range(30)]
        messages = self.go(self.state, page(*rows))
        self.assertEqual(len(messages), 1)
        self.assertIn("30 ta", messages[0])

    def test_extended_deadline_alerts(self):
        self.go(self.state, page(("Lecturer, CSE", "30/11/2026")))
        self.assertIn("Deadline bereche", "".join(self.go(self.state, page(("Lecturer, CSE", "10/12/2026")))))

    def test_one_day_timezone_shift_is_not_an_extension(self):
        self.go(self.state, page(("Lecturer, CSE", "30/11/2026")))
        self.assertEqual(self.go(self.state, page(("Lecturer, CSE", "01/12/2026"))), [])
        self.assertIn("2026-12-01", [r["deadline"] for r in self.state["items"].values()])

    def test_marketing_copy_and_email_do_not_alert(self):
        self.go(self.state, "<main><p>Welcome to the university portal</p></main>")
        html = ("<main><p>Welcome to the university portal</p><p>Explore exciting faculty and administrative positions</p>"
                "<p><a href='/contact'>recruitment@example.edu - Sat-Thu</a></p></main>")
        self.assertEqual(self.go(self.state, html), [])

    def test_deadline_from_detail_page_is_used(self):
        class Detail(FakeFetcher):
            def detail_deadlines(self, urls):
                return {u: date(2026, 12, 10) for u in urls}
        self.state["catchup_version"] = w.CATCHUP_VERSION
        html = "<main><table><tr><td><a href='/job/9'>Lecturer in Economics</a></td></tr></table></main>"
        notifier = w.DryRunSilent()
        w.run(CONFIG, self.state, Detail(html), notifier, NOW)
        self.assertIn("10 Dec 2026", "".join(notifier.messages))

    def test_catchup_is_sent_once_for_existing_memory(self):
        self.go(self.state, page(("Lecturer, CSE", "30/11/2026")))
        self.state.pop("catchup_version")
        self.assertIn("Updated list", "".join(self.go(self.state, page(("Lecturer, CSE", "30/11/2026")))))
        self.assertEqual(self.go(self.state, page(("Lecturer, CSE", "30/11/2026"))), [])

    def test_rule_upgrade_does_not_announce_old_content(self):
        html = page(("Lecturer, CSE", "30/11/2026"))
        self.go(self.state, html)
        self.state["rules_version"] = 0
        self.state["catchup_version"] = w.CATCHUP_VERSION
        wider = page(("Lecturer, CSE", "30/11/2026"), ("Old Accountant Post Notice", "01/01/2025"))
        self.assertEqual(self.go(self.state, wider), [])

    def test_broken_source_alerts_once_and_recovers(self):
        html = page(("Lecturer, CSE", "30/11/2026"))
        self.go(self.state, html)
        messages = []
        for _ in range(w.FAILS_BEFORE_ALERT + 2):
            messages += self.go(self.state, None)
        self.assertEqual(len(messages), 1)
        self.assertIn("Abar", "Abar " if "abar kaj" in "".join(self.go(self.state, html)) else "")

    def test_digest_reminder_once_per_threshold(self):
        self.state["last_digest"] = None
        html = page(("Lecturer, CSE", "26/11/2026"))
        first = self.go(self.state, html)
        self.assertTrue(any("Deadline reminder" in m for m in first))
        self.assertFalse(any("Deadline reminder" in m for m in self.go(self.state, html)))

    def test_unparsed_career_line_still_alerts(self):
        self.go(self.state, "<main><p>Join our team at this university today</p></main>")
        messages = self.go(self.state, "<main><p>Join our team at this university today</p><p>Lecturer in Economics</p></main>")
        self.assertEqual(len(messages), 1)
        self.assertIn("Lecturer in Economics", messages[0])

    def test_no_vacancy_message_removed_alerts(self):
        self.go(self.state, "<main><p>No Job Vacancy</p></main>")
        messages = self.go(self.state, "<main><p>Please see the attached document</p></main>")
        self.assertEqual(len(messages), 1)
        self.assertIn("no vacancy", messages[0])

    def test_same_circular_on_two_pages_alerts_once(self):
        config = {"universities": [{"name": "Example University", "urls": [],
                                    "monitor_urls": ["https://example.edu.bd/career", "https://example.edu.bd/jobs"]}]}
        class TwoPages:
            html = page(("Lecturer, CSE", "30/11/2026"))
            def __call__(self, sources):
                return {s["url"]: w.FetchResult(s["url"], True, "ok", self.html.replace("/job/", s["url"][-4:] + "/"), s["url"])
                        for s in sources}
        fetcher, notifier = TwoPages(), w.DryRunSilent()
        w.run(config, self.state, fetcher, notifier, NOW)
        notifier.messages.clear()
        fetcher.html = page(("Lecturer, CSE", "30/11/2026"), ("Assistant Registrar for Academic Affairs", "05/12/2026"))
        w.run(config, self.state, fetcher, notifier, NOW)
        self.assertEqual(sum("Assistant Registrar" in m for m in notifier.messages), 1)

    def test_weekly_list_includes_source_that_keeps_failing(self):
        state = w.new_state()
        state["sources"][PAGE] = {"fails": w.FAILS_BEFORE_ALERT}
        notifier = w.DryRunSilent()
        w.send_manual(CONFIG, notifier, state)
        self.assertIn("Example University", notifier.messages[0])

    def test_weekly_list_hides_suspicious_link(self):
        notifier = w.DryRunSilent()
        w.send_manual(CONFIG, notifier)
        self.assertIn("blocked.edu.bd", notifier.messages[0])
        self.assertNotIn("bad.example", notifier.messages[0])


class TelegramFormat(unittest.TestCase):
    def test_title_is_escaped(self):
        record = {"university": "A&B", "title": "<Lecturer>", "category": "Teaching", "deadline": None, "url": "https://a.edu/?a=1&b=2"}
        message = w.alert_message(record, date(2026, 11, 23))
        self.assertIn("A&amp;B", message)
        self.assertIn("&lt;Lecturer&gt;", message)
        self.assertIn("a=1&amp;b=2", message)

    def test_split(self):
        parts = w.split_message("\n".join(["x" * 100] * 100))
        self.assertTrue(all(len(p) <= 3900 for p in parts))
        self.assertEqual(sum(p.count("x" * 100) for p in parts), 100)


class Silent:
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)


w.DryRunSilent = Silent

if __name__ == "__main__":
    unittest.main()
