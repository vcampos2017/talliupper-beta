"""Regressions for item boundaries, slanted rows, and payment exclusions."""
import unittest
from receipt_reader.local_ocr import OcrLine, paddle_results_to_lines
from receipt_reader.parser import parse_receipt, join_price_rows

class ReceiptStructureTests(unittest.TestCase):
    def test_slanted_columns_preserve_four_items_and_totals(self):
        texts, polygons = [], []
        def add(text, x, y, width=400):
            texts.append(text)
            polygons.append([[x,y+.08*x],[x+width,y+.08*(x+width)],
                [x+width,y+24+.08*(x+width)],[x,y+24+.08*x]])
        add("HEB", 10, 0)
        for y, label, amount in [(60,"1 EGGS F","2.98"), (100,"2 AVOCADOS FW","2.68"),
            (180,"0.20 Lbs @ 1.24 FW","0.25"), (220,"4 SPARKLING BEVERAGE MINI CANS T","5.97"),
            (280,"Sale Subtotal***","11.88"), (320,"Sales Tax***","0.49"), (360,"Total Sale***","12.37"),
            (400,"*** MASTRCRD EFS","12.37"), (440,"ITEMS PURCHASED: 4","12.37"), (480,"YOU SAVED","1.60")]:
            add(label,10,y); add(amount,800,y,80)
        add("3 SWEET ONION",10,145)
        lines=paddle_results_to_lines([{"rec_texts":texts,"rec_scores":[.95]*len(texts),"rec_polys":polygons}])
        receipt=parse_receipt(lines).receipt
        self.assertEqual([i.line_total for i in receipt.items],[2.98,2.68,.25,5.97])
        self.assertEqual(receipt.items[3].name,"SPARKLING BEVERAGE MINI CANS")
        self.assertEqual((receipt.subtotal,receipt.tax,receipt.total),(11.88,.49,12.37))
        self.assertEqual([i.tax_marker for i in receipt.items],["F","FW","FW","T"])

    def test_summary_label_ends_items_even_without_amount(self):
        lines=[OcrLine(t,.9) for t in ["HEB","EGGS 2.98","Sale Subtotal***",
            "UNRECOGNIZED PAYMENT 12.37","YOU SAVED 1.60"]]
        self.assertEqual([i.name for i in parse_receipt(lines).receipt.items],["EGGS"])

    def test_payment_and_savings_are_never_items(self):
        lines=[OcrLine(t,.9) for t in ["HEB","EGGS 2.98","*** MASTRCRD EFS 12.37",
            "SALE & PRICE ITEMS PURCHASED: 4 12.37","YOU SAVED 1.60"]]
        self.assertEqual([i.name for i in parse_receipt(lines).receipt.items],["EGGS"])

    def test_neighbor_description_is_not_appended(self):
        lines=[OcrLine("AVOCADOS",.9,(10,10,200,30)), OcrLine("SWEET ONION",.9,(10,30,200,50)),
               OcrLine("2.68",.9,(300,20,360,40))]
        joined=join_price_rows(lines)
        self.assertFalse(any("AVOCADOS SWEET ONION" in l.text for l in joined))

if __name__ == "__main__":
    unittest.main()

