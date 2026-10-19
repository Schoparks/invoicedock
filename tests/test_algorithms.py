import itertools
import random
import unittest
from unittest.mock import patch
from io import BytesIO
import zipfile

from app.matching import best_combinations
from app.parsing import _ocr_config, _text_fields, expand_upload, parse_invoice


class MatchingTests(unittest.TestCase):
    def test_equal_amount_uses_own_sources_before_shared(self):
        invoices = [{'id':'foreign','amount_cents':1000,'source_priority':2},
                    {'id':'owned','amount_cents':1000,'source_priority':1},
                    {'id':'personal','amount_cents':1000,'source_priority':0}]
        for direction in ('above','below'):
            self.assertEqual(best_combinations(invoices,1000,direction=direction)[0]['invoice_ids'], ['personal'])
            self.assertEqual(best_combinations(invoices[:2],1000,direction=direction)[0]['invoice_ids'], ['owned'])
        invoices = [{'id':'personal','amount_cents':1200,'source_priority':0},
                    {'id':'f1','amount_cents':600,'source_priority':2},
                    {'id':'f2','amount_cents':600,'source_priority':2}]
        self.assertEqual(best_combinations(invoices,1000)[0]['invoice_ids'], ['personal'])
        invoices.append({'id':'exact','amount_cents':1000,'source_priority':2})
        self.assertEqual(best_combinations(invoices,1000)[0]['invoice_ids'], ['exact'])

    def test_source_priority_matches_exhaustive_subsets(self):
        rng = random.Random(19)
        for _ in range(35):
            invoices = [{'id':str(i),'amount_cents':rng.randint(1,20),'source_priority':rng.randrange(3)} for i in range(7)]
            target = rng.randint(1,60)
            subsets = [list(combo) for n in range(1,8) for combo in itertools.combinations(invoices,n)]
            for direction in ('above','below'):
                eligible = [combo for combo in subsets if (sum(i['amount_cents'] for i in combo)>=target if direction=='above' else sum(i['amount_cents'] for i in combo)<=target)]
                def cost(combo):
                    return (sum(i['amount_cents'] for i in combo) * (-1 if direction=='below' else 1),
                            sum(i['amount_cents'] for i in combo if i['source_priority']==2),
                            sum(i['amount_cents'] for i in combo if i['source_priority']==1))
                result = best_combinations(invoices,target,limit=1,direction=direction)
                if not eligible:
                    self.assertEqual(result,[])
                else:
                    selected = [i for i in invoices if i['id'] in result[0]['invoice_ids']]
                    self.assertEqual(cost(selected),min(map(cost,eligible)))

    def test_exact_match_and_no_reuse(self):
        invoices = [{"id": str(i), "amount_cents": amount} for i, amount in enumerate([681, 320, 319, 500])]
        results = best_combinations(invoices, 1000)
        self.assertEqual(results[0]["total_cents"], 1000)
        self.assertEqual(results[0]["gap_cents"], 0)
        self.assertEqual(len({frozenset(row["invoice_ids"]) for row in results}), len(results))
        for result in results:
            self.assertEqual(len(result["invoice_ids"]), len(set(result["invoice_ids"])))
            self.assertGreaterEqual(result["total_cents"], 1000)

    def test_first_result_matches_exhaustive_subsets(self):
        rng = random.Random(27)
        for _ in range(30):
            values = [rng.randint(1, 70) for _ in range(9)]
            target = rng.randint(1, 230)
            invoices = [{"id": i, "amount_cents": value} for i, value in enumerate(values)]
            totals = [sum(value for value, use in zip(values, choice) if use)
                      for choice in itertools.product((False, True), repeat=len(values))]
            for direction in ("above", "below"):
                with self.subTest(direction=direction, target=target, values=values):
                    expected = (min((total for total in totals if total >= target), default=0)
                                if direction == "above" else max(total for total in totals if total <= target))
                    result = best_combinations(invoices, target, limit=1, direction=direction)
                    actual = result[0]["total_cents"] if result else 0
                    self.assertEqual(actual, expected)
                    if result:
                        self.assertEqual(sum(values[i] for i in result[0]["invoice_ids"]), expected)

    def test_closest_at_or_above_target(self):
        cases = [
            ([700, 400, 1500], 1000, 1100),
            ([1100], 1000, 1100),
            ([600, 600, 1100], 1000, 1100),
            ([601, 400, 1100], 1000, 1001),
            ([600, 600, 1101], 1000, 1101),
            ([600, 600], 1000, 1200),
            ([100_000_000, 601, 400], 1000, 1001),
            ([100_000_000], 1, 100_000_000),
            ([400, 599], 1000, 0),
            ([], 1000, 0),
            ([100], 0, 0),
        ]
        for values, target, expected in cases:
            with self.subTest(values=values, target=target):
                invoices = [{"id": i, "amount_cents": value} for i, value in enumerate(values)]
                result = best_combinations(invoices, target)
                self.assertEqual(result[0]["total_cents"] if result else 0, expected)
                self.assertEqual([row["total_cents"] for row in result],
                                 sorted(row["total_cents"] for row in result))
                for row in result:
                    self.assertGreaterEqual(row["total_cents"], target)
                    self.assertEqual(row["gap_cents"], target - row["total_cents"])
                    self.assertEqual(sum(values[i] for i in row["invoice_ids"]), row["total_cents"])

    def test_closest_at_or_below_target(self):
        cases = [
            ([700, 300, 299, 1001], 1000, 1000),
            ([1000], 1000, 1000),
            ([700, 299, 1001], 1000, 999),
            ([600, 600], 1000, 600),
            ([300], 1000, 300),
            ([1001], 1000, 0),
            ([1], 1, 1),
            ([1], 0, 0),
        ]
        for values, target, expected in cases:
            with self.subTest(values=values, target=target):
                invoices = [{"id": i, "amount_cents": value} for i, value in enumerate(values)]
                results = best_combinations(invoices, target, direction="below")
                self.assertEqual(results[0]["total_cents"] if results else 0, expected)
                self.assertEqual([row["total_cents"] for row in results],
                                 sorted((row["total_cents"] for row in results), reverse=True))
                self.assertEqual(len({frozenset(row["invoice_ids"]) for row in results}), len(results))
                for row in results:
                    self.assertLessEqual(row["total_cents"], target)
                    self.assertGreater(row["total_cents"], 0)
                    self.assertEqual(len(row["invoice_ids"]), len(set(row["invoice_ids"])))
                    self.assertEqual(sum(values[i] for i in row["invoice_ids"]), row["total_cents"])
                    self.assertEqual(row["gap_cents"], target - row["total_cents"])

    def test_limits_and_nonpositive(self):
        with self.assertRaises(ValueError):
            best_combinations([], 100, direction="invalid")
        self.assertEqual(best_combinations([{"id": 1, "amount_cents": 0}, {"id": 2, "amount_cents": -10}], 100), [])
        with self.assertRaises(ValueError):
            best_combinations([], 10_000_001)
        with self.assertRaises(ValueError):
            best_combinations([{"id": i, "amount_cents": 1} for i in range(1001)], 100)
        with self.assertRaises(ValueError):
            best_combinations([{"id": 1, "amount_cents": 1.2}], 100)


class ParsingTests(unittest.TestCase):
    def test_structured_invoice_and_pending_note(self):
        xml = '''<Invoice><InvoiceNo>12345678901234567890</InvoiceNo><SellerName>测试餐饮公司</SellerName>
          <BuyerName>某某公司</BuyerName><InvoiceDate>2026年10月15日</InvoiceDate>
          <TotalTaxIncludedAmount>1,234.56</TotalTaxIncludedAmount><Amount>999.00</Amount></Invoice>'''.encode()
        result = parse_invoice(xml, "电子发票.xml")
        self.assertEqual(result["amount_cents"], 123456)
        self.assertEqual(result["date"], "2026-10-15")
        self.assertEqual(result["seller"], "测试餐饮公司")
        self.assertEqual(result["buyer"], "某某公司")
        self.assertEqual(result["category"], "餐饮")
        self.assertIn("核对", result["review_note"])

    def test_not_guessing_subtotal_or_invalid_date(self):
        result = parse_invoice(b'<Invoice><Amount>999.00</Amount><InvoiceDate>2026-02-30</InvoiceDate></Invoice>', "invoice.xml")
        self.assertEqual(result["amount_cents"], 0)
        self.assertEqual(result["date"], "")
        with self.assertRaises(ValueError):
            parse_invoice(b'<!DOCTYPE root [<!ENTITY a SYSTEM "file:///etc/passwd">]><root>&a;</root>', "invoice.xml")

    def test_vertical_party_labels_and_xml_issue_time(self):
        fields = _text_fields("购 名称：测试买方有限公司    销 名称：测试卖方有限公司\n买    售\n方    方\n信    信\n息    息")
        self.assertEqual(fields['buyer'], '测试买方有限公司')
        self.assertEqual(fields['seller'], '测试卖方有限公司')
        result = parse_invoice(b'<Invoice><IssueTime>2026-01-15</IssueTime><RequestTime>2026-01-16</RequestTime></Invoice>', 'test.xml')
        self.assertEqual(result['date'], '2026-01-15')

    def test_ofd_resolves_semantic_object_references_without_glyph_metadata(self):
        page = '''<Page><TextObject ID="1"><CGTransform><Glyphs>9 8 7</Glyphs></CGTransform><TextCode>26612000000000000001</TextCode></TextObject>
          <TextObject ID="2"><TextCode>2026年01月15日</TextCode></TextObject>
          <TextObject ID="3"><TextCode>测试卖方</TextCode></TextObject><TextObject ID="4"><TextCode>测试买方</TextCode></TextObject>
          <TextObject ID="5"><TextCode>¥</TextCode></TextObject><TextObject ID="6"><TextCode>123.45</TextCode></TextObject></Page>'''
        tags = '''<root><InvoiceNo><ObjectRef PageRef="p1">1</ObjectRef></InvoiceNo><IssueDate><ObjectRef PageRef="p1">2</ObjectRef></IssueDate>
          <SellerName><ObjectRef PageRef="p1">3</ObjectRef></SellerName><BuyerName><ObjectRef PageRef="p1">4</ObjectRef></BuyerName>
          <TaxInclusiveTotalAmount><ObjectRef PageRef="p1">5</ObjectRef><ObjectRef PageRef="p1">6</ObjectRef></TaxInclusiveTotalAmount></root>'''
        content = self.zip_bytes([('Doc_0/Pages/Page_0/Content.xml', page), ('Doc_0/Tags/CustomTag.xml', tags),
                                  ('Doc_0/Document.xml', '<Document><Pages><Page ID="p1" BaseLoc="Pages/Page_0/Content.xml"/></Pages></Document>')])
        result = parse_invoice(content, 'test.ofd')
        self.assertEqual((result['number'], result['date'], result['amount_cents']), ('26612000000000000001', '2026-01-15', 12345))
        self.assertEqual((result['seller'], result['buyer']), ('测试卖方', '测试买方'))
        self.assertNotIn('Glyphs', result['raw_text'])

    def test_missing_ocr_keeps_valid_image_and_scanned_pdf(self):
        import pymupdf
        from PIL import Image
        stream = BytesIO()
        with Image.new("RGB", (100, 100), "white") as image:
            image.save(stream, format="PNG")
        with pymupdf.open() as document:
            document.new_page(width=100, height=100)
            pdf = document.tobytes()
        _ocr_config.cache_clear()
        try:
            with patch("app.parsing.importlib.util.find_spec", return_value=None):
                for filename, content in (("scan.png", stream.getvalue()), ("scan.pdf", pdf)):
                    with self.subTest(filename=filename):
                        result = parse_invoice(content, filename)
                        self.assertEqual(result["raw_text"], "")
                        self.assertEqual(result["amount_cents"], 0)
                        self.assertIn("手动填写", result["review_note"])
                        self.assertIn("OCR 不可用", result["source"])
                with self.assertRaises(ValueError):
                    parse_invoice(b"broken image", "broken.png")
        finally:
            _ocr_config.cache_clear()

    def zip_bytes(self, entries):
        data = BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            for name, value in entries:
                archive.writestr(name, value)
        return data.getvalue()

    def test_zip_expansion_and_safety(self):
        payload = b'<Invoice><InvoiceNo>12345678</InvoiceNo></Invoice>'
        content = self.zip_bytes([("folder/invoice.xml", payload), ("__MACOSX/._invoice.xml", b"junk")])
        self.assertEqual(expand_upload(content, "upload.zip"), [("folder/invoice.xml", payload)])
        for name in ("../outside.xml", "/outside.xml", "C:/outside.xml", "inner.zip"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                expand_upload(self.zip_bytes([(name, payload)]), "upload.zip")

    def test_ofd_text_extraction_is_explicitly_limited(self):
        content = self.zip_bytes([("Doc_0/Pages/Page_0/Content.xml", '<ofd:Page xmlns:ofd="urn:ofd"><ofd:TextCode>价税合计：123.45</ofd:TextCode></ofd:Page>'.encode())])
        result = parse_invoice(content, "invoice.ofd")
        self.assertEqual(result["amount_cents"], 12345)
        self.assertIn("可能无法识别", result["review_note"])


if __name__ == "__main__":
    unittest.main()
