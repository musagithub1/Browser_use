import unittest

from jev_linux.spans import text_candidates, url_candidates


class TextCandidateTests(unittest.TestCase):
    def test_existing_common_dictation_has_stable_first_candidate(self):
        for command, content in (("open notes and type hello world", "hello world"),
                                 ('type "hello" into the search box', "hello"),
                                 ("type hello and press enter", "hello")):
            with self.subTest(command=command):
                self.assertEqual(text_candidates(command)["s0"], content)

    def test_multiple_note_commands_extract_each_exact_payload(self):
        command = "create a note containing first; then create another note containing second and then create a third note saying third"
        values = list(text_candidates(command).values())
        self.assertEqual(values[:3], ["first", "second", "third"])
        self.assertNotIn("first;", values)
        self.assertNotIn("first; then create another note containing second", values)

    def test_all_payload_pattern_matches_are_used_not_only_first(self):
        command = "type first and then type second; append third to the same note; then add fourth to that existing note"
        values = list(text_candidates(command).values())
        self.assertEqual(values[:4], ["first", "second", "third", "fourth"])

    def test_sequential_markers_only_split_when_an_operation_follows(self):
        for command, content in (("append bread and butter to the same note", "bread and butter"),
                                 ("type now and then we relax", "now and then we relax"),
                                 ("type red; green; blue", "red; green; blue"),
                                 ("type first then press Enter", "first"),
                                 ("type first;press Enter", "first"),
                                 ("type first; and then press Enter", "first")):
            with self.subTest(command=command):
                self.assertEqual(text_candidates(command)["s0"], content)

    def test_quotes_protect_semicolons_conjunctions_and_destination_words(self):
        command = ('create a note containing "bread and then press Enter; then add butter into the search field" '
                   'and then append "This belongs to the same note" to that note')
        values = list(text_candidates(command).values())
        self.assertEqual(values[:2], ["bread and then press Enter; then add butter into the search field",
                                     "This belongs to the same note"])
        self.assertNotIn("This belongs", values)
        self.assertNotIn("Enter; then add butter", values)

    def test_curly_and_single_quotes_preserve_literal_spacing_and_line_breaks(self):
        values = text_candidates("type “  Café  🍵\nsecond line  ” and then append 'Bread and butter' to the note")
        self.assertEqual(values["s0"], "  Café  🍵\nsecond line  ")
        self.assertEqual(values["s1"], "Bread and butter")

    def test_apostrophes_and_escaped_quotes_do_not_create_false_boundaries(self):
        command = "type Don't forget John's notes and then press Enter"
        self.assertEqual(text_candidates(command)["s0"], "Don't forget John's notes")
        literal = r'He said \"press Enter; then add butter\".'
        values = text_candidates('type "' + literal + '" and then press Enter')
        self.assertEqual(values["s0"], literal)

    def test_destination_suffixes_are_removed_from_unquoted_payloads(self):
        for destination in ("to the same note", "to that existing note", "to an existing note",
                            "into the search field", "into searchfield", "in the document", "to my current note."):
            with self.subTest(destination=destination):
                self.assertEqual(text_candidates("append exact words " + destination)["s0"], "exact words")

    def test_drafting_topic_search_and_title_patterns_keep_each_match(self):
        commands = ("create a note about gardening; then create another note about cooking",
                    "search the web for mountain weather and then search for train times",
                    "create a note called Groceries; then create another note named Ideas")
        expected = (["gardening", "cooking"], ["mountain weather", "train times"], ["Groceries", "Ideas"])
        for command, contents in zip(commands, expected):
            with self.subTest(command=command):
                self.assertEqual(list(text_candidates(command).values())[:2], contents)

    def test_candidates_are_deduplicated_bounded_and_deterministic(self):
        command = "; then ".join(f'type "payload {index}"' for index in range(120))
        first = text_candidates(command)
        self.assertEqual(first, text_candidates(command))
        self.assertEqual(len(first), 80)
        self.assertEqual(list(first), [f"s{index}" for index in range(80)])
        self.assertEqual(first["s79"], "payload 79")
        self.assertEqual(text_candidates('type "same" and then append "same"')["s0"], "same")


class UrlCandidateTests(unittest.TestCase):
    def test_spoken_domain_and_common_site_names(self):
        self.assertEqual(url_candidates("open example dot com"), {"u0": "https://example.com"})
        self.assertEqual(url_candidates("open google and then github"),
                         {"u0": "https://google.com", "u1": "https://github.com"})

    def test_http_scheme_case_is_normalized_without_changing_path_case(self):
        self.assertEqual(url_candidates("open HTTPS://Example.com/CaseSensitive?Q=Hello"),
                         {"u0": "https://Example.com/CaseSensitive?Q=Hello"})
        self.assertEqual(url_candidates("open HTTP://example.com/a"), {"u0": "http://example.com/a"})

    def test_sentence_punctuation_is_removed(self):
        for punctuation in (".", ",", "!", "?", ")", ").", ";"):
            with self.subTest(punctuation=punctuation):
                self.assertEqual(url_candidates("visit https://example.com/article" + punctuation),
                                 {"u0": "https://example.com/article"})

    def test_balanced_path_parentheses_and_internal_punctuation_are_preserved(self):
        self.assertEqual(url_candidates("visit (https://en.wikipedia.org/wiki/Function_(mathematics))."),
                         {"u0": "https://en.wikipedia.org/wiki/Function_(mathematics)"})
        self.assertEqual(url_candidates("read https://example.com/a,b;c?q=one!two&v=a.b#section."),
                         {"u0": "https://example.com/a,b;c?q=one!two&v=a.b#section"})

    def test_bare_domain_queries_ports_and_fragments_are_not_lost(self):
        self.assertEqual(url_candidates("open example.com:8080?q=hello#part"),
                         {"u0": "https://example.com:8080?q=hello#part"})

    def test_explicit_url_does_not_add_an_unrequested_site_homepage(self):
        self.assertEqual(url_candidates("open https://github.com/org/project."),
                         {"u0": "https://github.com/org/project"})

    def test_email_and_unsupported_scheme_are_not_reinterpreted_as_web_urls(self):
        self.assertEqual(url_candidates("email person@example.com"), {})
        self.assertEqual(url_candidates("open ftp://example.com"), {})

    def test_url_candidates_are_deduplicated_and_bounded(self):
        self.assertEqual(url_candidates("open HTTPS://example.com and https://example.com"),
                         {"u0": "https://example.com"})
        result = url_candidates(" ".join(f"https://example.com/{index}" for index in range(100)))
        self.assertEqual(len(result), 80)
        self.assertEqual(list(result), [f"u{index}" for index in range(80)])


if __name__ == "__main__":
    unittest.main()
