import unittest
from datetime import date, datetime, timedelta, timezone

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
    def send(self, text, silent=False):
        raise w.TelegramError("down")


CONFIG = {"universities": [{"name": "Example University", "monitor_urls": [PAGE], "urls": [PAGE], "status": "Ready",
                            "type": "private"},
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

    def test_apply_to_website_by_date(self):
        self.assertEqual(w.find_deadline("Applicants must apply to www.career.uits.ac.bd by the 25 November, 2026."),
                         date(2026, 11, 25))

    def test_circular_text_says_which_board(self):
        self.assertEqual(w.circular_facts("Lecturer (CSE) ... Registrar")["groups"], ["lecturer"])
        self.assertEqual(w.circular_facts("Section Officer, Assistant Registrar")["groups"], ["officer"])
        self.assertEqual(w.circular_facts("Lecturer in Law. Registrar")["groups"], [])

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

    def test_focus_keeps_cse_bba_lecturer_ra_and_officers(self):
        for title in ["Lecturer (CSE)", "Lecturer, Department of Business Administration", "Research Assistant, Computer Science",
                      "Lecturer (Supply Chain Management)", "Faculty Search", "Junior Administrative Officer",
                      "Section Officer", "Assistant Registrar", "IT Officer", "Job Opportunity for Post of Teachers"]:
            self.assertTrue(w.wanted_post(title, title), title)

    def test_focus_drops_other_posts(self):
        for title in ["Lecturer (Law & Human Rights)", "Lecturer, Department of Japanese Studies", "Assistant Professor (CSE)",
                      "Professor, Department of Clinical Pharmacy", "Medical Officer", "Engineer (Civil)",
                      "School of Law - Faculty Search", "Research Assistant, Physics"]:
            self.assertFalse(w.wanted_post(title, title), title)
        self.assertFalse(w.wanted_post("Lecturer (English)", "Lecturer (English) JU School and College"))

    def test_menu_word_without_deadline_is_ignored(self):
        self.assertIsNone(w.classify("Lecturer", "Lecturer", PAGE))

    def test_date_is_removed_from_title(self):
        self.assertEqual(w.tidy_title("Faculty Search [Posted: November 04, 2026] [Deadline: November 30, 2026]"), "Faculty Search")

    def test_clerical_post_in_registrar_office_is_skipped(self):
        self.assertIsNone(w.classify("উচ্চমান সহকারী, Office of the Registrar (01 post)", "Deadline: 15/12/2026", PAGE))

    def test_malformed_link_does_not_lose_the_page(self):
        html = '<a href="http://[broken">x</a><div><a href="/c.pdf">Lecturer (CSE) Recruitment</a></div>'
        self.assertEqual([i.title for i in w.extract_items("U", html, PAGE)], ["Lecturer (CSE) Recruitment"])

    def test_web_app_job_routes_stay_separate(self):
        html = ('<div><a href="#/jobs-details/1">Senior Manager, Procurement</a><p>30 November 2026 8 Days Remaining</p></div>'
                '<div><a href="#/jobs-details/2">Assistant Director, Energy</a><p>24 November 2026 2 Days Remaining</p></div>'
                '<div><a href="#/jobs-details/1">Senior Manager, Procurement Dept: Procurement</a></div>')
        items = w.extract_items("U", html, "https://career.example.edu/#/available-jobs")
        self.assertEqual(sorted(i.title for i in items), ["Assistant Director, Energy", "Senior Manager, Procurement"])
        self.assertEqual({i.deadline for i in items}, {date(2026, 11, 30), date(2026, 11, 24)})

    def test_position_accordion(self):
        html = ('<div><strong>Position: Lecturer (CSE, Business Administration)</strong><p>Requirements</p>'
                '<p>Deadline: September 12, 2026</p></div>')
        item = next(i for i in w.extract_items("U", html, PAGE) if i.title.startswith("Lecturer"))
        self.assertEqual(item.deadline, date(2026, 9, 12))

    def test_circular_published_as_image(self):
        html = '<div><img alt="AUB Job Circular" src="/c.jpg"><p>Application Deadline</p><p>30 OCT 2026</p></div>'
        item = w.extract_items("U", html, PAGE)[0]
        self.assertEqual(item.deadline, date(2026, 10, 30))
        self.assertEqual(w.classify(item.title, item.context, PAGE)[0], "strong")

    def test_browse_button_is_not_a_title(self):
        html = '<table><tr><td>2</td><td>Lecturer (Department of CSE)</td><td><a href="https://x.test/j/1">Browse</a></td></tr></table>'
        self.assertEqual(w.extract_items("U", html, PAGE)[0].title, "Lecturer (Department of CSE)")

    def test_manual_check_site_is_in_weekly_list(self):
        config = {"universities": [{"name": "Poster Uni", "urls": ["https://p.test/"], "monitor_urls": ["https://p.test/"],
                                    "manual_check": True},
                                   {"name": "Normal Uni", "urls": ["https://n.test/"], "monitor_urls": ["https://n.test/"]}]}
        text = "\n".join(w.manual_lines(config, {"sources": {}}))
        self.assertIn("Poster Uni", text)
        self.assertNotIn("Normal Uni", text)


DAY = timedelta(days=1)


class Flow(unittest.TestCase):
    def go(self, html, now=NOW, config=CONFIG, fetcher=None):
        self.notifier = Silent()
        w.run(config, self.state, fetcher or FakeFetcher(html), self.notifier, now)
        return self.notifier.messages

    def setUp(self):
        self.state = w.new_state()

    def test_two_boards_every_hour(self):
        html = page(("Lecturer, CSE", "30/11/2026"), ("Lecturer, BBA", "01/01/2025"), ("Assistant Registrar", "05/12/2026"))
        for _ in range(2):
            lecturer, officer = self.go(html)
            self.assertIn("LECTURER", lecturer)
            self.assertIn("Lecturer, CSE", lecturer)
            self.assertNotIn("Lecturer, BBA", lecturer)  # deadline already over
            self.assertNotIn("NEW</b>", lecturer)  # already up when the bot started watching
            self.assertIn("Assistant Registrar", officer)
            self.assertNotIn("Assistant Registrar", lecturer)

    def test_new_tag_only_on_publish_day(self):
        self.go(page(("Lecturer, CSE", "30/11/2026")))
        html = page(("Lecturer, CSE", "30/11/2026"), ("Research Assistant, Computer Science", "05/12/2026"))
        lecturer = self.go(html, NOW + timedelta(hours=1))[0]
        self.assertIn("NEW</b> · <b>Example University</b> — <a href=\"https://example.edu.bd/job/1\">Research", lecturer)
        self.assertNotIn("NEW</b> · <b>Example University</b> — <a href=\"https://example.edu.bd/job/0\"", lecturer)
        self.assertIn("NEW</b> · <b>Example", self.go(html, NOW + timedelta(hours=5))[0])  # still the same day
        self.assertNotIn("NEW</b>", self.go(html, NOW + DAY)[0])

    def test_expired_circular_disappears(self):
        html = page(("Lecturer, CSE", "23/11/2026"))
        self.assertIn("aj shesh din", self.go(html)[0])
        self.assertNotIn("Lecturer, CSE", self.go(html, NOW + DAY)[0])

    def test_sorted_by_deadline(self):
        lecturer = self.go(page(("Lecturer, Marketing", "20/12/2026"), ("Lecturer, CSE", "25/11/2026"),
                                ("Lecturer, Finance", "01/12/2026")))[0]
        self.assertLess(lecturer.index("CSE"), lecturer.index("Finance"))
        self.assertLess(lecturer.index("Finance"), lecturer.index("Marketing"))
        self.assertIn("🔴", lecturer)

    def test_private_and_public_sections(self):
        config = {"universities": [dict(CONFIG["universities"][0], type="private"),
                                   {"name": "State University", "type": "public", "urls": [],
                                    "monitor_urls": ["https://state.edu.bd/career"]}], "backup_sources": []}
        lecturer, officer = self.go(page(("Lecturer, CSE", "30/11/2026"), ("Section Officer", "30/11/2026")), config=config)
        private, public = lecturer.split("PUBLIC UNIVERSITY")
        self.assertIn("Example University", private)
        self.assertIn("State University", public)
        self.assertIn("[Public]", officer)
        self.assertIn("[Private]", officer)

    def test_silent_unless_something_new(self):
        html = page(("Lecturer, CSE", "30/11/2026"))
        self.go(html)
        self.go(html)
        self.assertEqual(self.notifier.silent, [True, True])
        self.go(page(("Lecturer, CSE", "30/11/2026"), ("Assistant Registrar", "05/12/2026")))
        self.assertEqual(self.notifier.silent, [False, False])

    def test_previous_boards_are_deleted(self):
        html = page(("Lecturer, CSE", "30/11/2026"))
        self.go(html)
        first = list(self.state["board_messages"])
        self.go(html)
        self.assertEqual(self.notifier.deleted, first)
        self.assertNotEqual(self.state["board_messages"], first)

    def test_failed_telegram_is_loud_next_run(self):
        self.go(page(("Lecturer, CSE", "30/11/2026")))
        html = page(("Lecturer, CSE", "30/11/2026"), ("Assistant Registrar", "05/12/2026"))
        with self.assertRaises(w.TelegramError):
            w.run(CONFIG, self.state, FakeFetcher(html), FailingNotifier(), NOW)
        self.assertIn("Assistant Registrar", self.go(html)[1])
        self.assertEqual(self.notifier.silent, [False, False])

    def test_redesign_flood_stays_off_the_board(self):
        self.go(page(("Lecturer, CSE", "30/11/2026")))
        lecturer = self.go(page(*[(f"Lecturer Post {i}", "30/12/2026") for i in range(30)]))[0]
        self.assertNotIn("Lecturer Post 5", lecturer)

    def test_extended_deadline_is_marked(self):
        self.go(page(("Lecturer, CSE", "30/11/2026")))
        self.assertIn("deadline bereche", self.go(page(("Lecturer, CSE", "10/12/2026")))[0])

    def test_one_day_timezone_shift_is_not_an_extension(self):
        self.go(page(("Lecturer, CSE", "30/11/2026")))
        self.assertNotIn("bereche", self.go(page(("Lecturer, CSE", "01/12/2026")))[0])
        self.assertIn("2026-12-01", [r["deadline"] for r in self.state["items"].values()])

    def test_marketing_copy_and_email_are_not_listed(self):
        self.go("<main><p>Welcome to the university portal</p></main>")
        html = ("<main><p>Welcome to the university portal</p><p>Explore exciting faculty and administrative positions</p>"
                "<p><a href='/contact'>recruitment@example.edu - Sat-Thu</a></p></main>")
        lecturer, officer = self.go(html)
        self.assertIn("Ekhon kono open circular nei", lecturer)
        self.assertNotIn("Explore", lecturer + officer)
        self.assertNotIn("recruitment@", lecturer + officer)

    def test_deadline_from_detail_page_is_used(self):
        class Detail(FakeFetcher):
            def detail_info(self, urls):
                return {u: {"deadline": "2026-12-10", "groups": []} for u in urls}
        html = "<main><table><tr><td><a href='/job/9'>Lecturer in Marketing</a></td></tr></table></main>"
        self.assertIn("10 Dec", self.go(html, fetcher=Detail(html))[0])

    def test_general_pdf_with_lecturer_and_officer_posts_is_on_both_boards(self):
        text = ("Recruitment Notice Lecturer Computer Science and Engineering Business Studies Section Officer "
                "Applicants must apply to www.career.uits.ac.bd by the 25 November, 2026. Registrar")
        class Pdf(FakeFetcher):
            def detail_info(self, urls):
                return {u: w.circular_facts(text) for u in urls}
        html = "<nav><a href='/front/images/circular_11_11_2026.pdf'>Download Job Circular</a></nav><main><p>Apply</p></main>"
        for _ in range(2):  # already-known circulars are read again too
            lecturer, officer = self.go(html, fetcher=Pdf(html))
            self.assertIn("Job Circular (11 Nov 2026)", lecturer)
            self.assertIn("Job Circular (11 Nov 2026)", officer)
            self.assertIn("25 Nov (2 din baki)", lecturer)

    def test_rule_upgrade_does_not_mark_old_content_new(self):
        html = page(("Lecturer, CSE", "30/11/2026"))
        self.go(html)
        self.state["rules_version"] = 0
        self.go(page(("Lecturer, CSE", "30/11/2026"), ("Assistant Accountant Post", "30/12/2026")))
        self.assertEqual(self.notifier.silent, [True, True])

    def test_broken_source_is_listed_until_it_recovers(self):
        html = page(("Lecturer, CSE", "30/11/2026"))
        self.go(html)
        for _ in range(w.FAILS_BEFORE_ALERT):
            officer = self.go(None)[1]
        self.assertIn("check kora jachhe na", officer)
        self.assertNotIn("check kora jachhe na", self.go(html)[1])

    def test_new_career_line_without_deadline(self):
        self.go("<main><p>Join our team at this university today</p></main>")
        lecturer = self.go("<main><p>Join our team at this university today</p><p>Lecturer in Marketing</p></main>")[0]
        self.assertIn("Deadline lekha nai", lecturer)
        self.assertIn("NEW</b> · <b>Example University</b> — <a href=\"https://example.edu.bd/career\">Lecturer in Marketing", lecturer)
        later = "<main><p>Join our team at this university today</p><p>Lecturer in Marketing</p></main>"
        self.assertNotIn("Marketing", self.go(later, NOW + timedelta(days=w.UNDATED_DAYS + 1))[0])

    def test_undated_post_that_was_already_there_is_hidden(self):
        lecturer = self.go("<main><p>Join our team at this university today</p><p>Lecturer in Marketing</p></main>")[0]
        self.assertNotIn("Marketing", lecturer)

    def test_no_vacancy_message_removed_is_listed(self):
        self.go("<main><p>No Job Vacancy</p></main>")
        self.assertIn("no vacancy", self.go("<main><p>Please see the attached document</p></main>")[1])

    def test_same_circular_on_two_pages_is_listed_once(self):
        config = {"universities": [{"name": "Example University", "urls": [],
                                    "monitor_urls": ["https://example.edu.bd/career", "https://example.edu.bd/jobs"]}]}
        class TwoPages:
            html = page(("Lecturer, CSE", "30/11/2026"))
            def __call__(self, sources):
                return {s["url"]: w.FetchResult(s["url"], True, "ok", self.html.replace("/job/", s["url"][-4:] + "/"), s["url"])
                        for s in sources}
        fetcher = TwoPages()
        self.go(None, config=config, fetcher=fetcher)
        fetcher.html = page(("Lecturer, CSE", "30/11/2026"), ("Assistant Registrar for Academic Affairs", "05/12/2026"))
        self.assertEqual(self.go(None, config=config, fetcher=fetcher)[1].count("Assistant Registrar"), 1)

    def test_sunday_board_carries_the_weekly_list_once(self):
        sunday = NOW - DAY
        html = page(("Lecturer, CSE", "30/11/2026"))
        officer = self.go(html, sunday)[1]
        self.assertIn("blocked.edu.bd", officer)
        self.assertNotIn("bad.example", officer)
        self.assertNotIn("Weekly manual", self.go(html, sunday + timedelta(hours=1))[1])

    def test_weekly_list_includes_source_that_keeps_failing(self):
        state = {"sources": {PAGE: {"fails": w.FAILS_BEFORE_ALERT}}}
        self.assertIn("Example University", "\n".join(w.manual_lines(CONFIG, state)))


class TelegramFormat(unittest.TestCase):
    def record(self, title, deadline, first_seen="2026-11-01", university="Example University"):
        return {"university": university, "title": title, "category": "Officer/Admin", "deadline": deadline,
                "url": "https://a.edu/j", "first_seen": first_seen, "level": "strong", "baseline": False}

    def test_title_is_escaped(self):
        record = dict(self.record("<Lecturer>", "2026-11-30", university="A&B"), url="https://a.edu/?a=1&b=2")
        line = w.board_line(record, date(2026, 11, 23), {}, False)
        self.assertIn("A&amp;B", line)
        self.assertIn("&lt;Lecturer&gt;", line)
        self.assertIn("a=1&amp;b=2", line)

    def test_new_tag_only_on_items_first_seen_today(self):
        today = date(2026, 11, 24)
        self.assertIn("NEW", w.board_line(self.record("Officer", "2026-11-30", "2026-11-24"), today, {}, False))
        self.assertNotIn("NEW", w.board_line(self.record("Officer", "2026-11-30"), today, {}, False))

    def test_same_circular_is_listed_once(self):
        same = self.record("Job Opportunity for Officers", "2026-11-30")
        self.assertEqual(len(w.unique_records([same, dict(same, url="https://a.edu/other")])), 1)

    def test_changed_deadline_keeps_only_the_newest(self):
        old = self.record("Lecturer (CSE)", "2026-11-30", "2026-11-23")
        new = self.record("Lecturer (CSE)", "2026-12-03", "2026-11-24")
        self.assertEqual(w.latest_versions([old, new]), [new])

    def test_long_quote_is_split_with_closed_tags(self):
        blocks = w.quote_blocks("Heading", [f"• line {i} " + "x" * 200 for i in range(40)])
        self.assertGreater(len(blocks), 1)
        for block in blocks:
            self.assertLessEqual(len(block), 3900)
            self.assertEqual(block.count("<blockquote expandable>"), block.count("</blockquote>"))
        parts = w.split_message("\n\n".join(blocks))
        self.assertTrue(all(p.count("<blockquote") == p.count("</blockquote>") for p in parts))

    def test_split_keeps_a_circular_together(self):
        blocks = [f"title {i}\nplace {i}" + "x" * 300 for i in range(40)]
        parts = w.split_message("\n\n".join(blocks))
        self.assertGreater(len(parts), 1)
        for i in range(40):
            self.assertEqual(sum(f"title {i}\nplace {i}" in p for p in parts), 1)

    def test_split(self):
        parts = w.split_message("\n".join(["x" * 100] * 100))
        self.assertTrue(all(len(p) <= 3900 for p in parts))
        self.assertEqual(sum(p.count("x" * 100) for p in parts), 100)


class Silent:
    def __init__(self):
        self.messages, self.silent, self.deleted = [], [], []

    def send(self, text, silent=False):
        self.messages.append(text)
        self.silent.append(silent)
        return [id(text)]

    def delete(self, ids):
        self.deleted += ids


if __name__ == "__main__":
    unittest.main()
