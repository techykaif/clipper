import json
import unittest

from clipper import ai_review


def payload(verdict="KEEP", score=82):
    return {
        "verdict": verdict,
        "score": score,
        "summary": "A clear and entertaining gameplay moment.",
        "strengths": ["Clear event", "Good payoff"],
        "issues": [],
        "moments": [{"start": 4.0, "end": 8.5, "observation": "The event resolves."}],
    }


class AntigravityReviewValidationTests(unittest.TestCase):
    def test_parses_success_envelope(self):
        result = ai_review._parse(json.dumps({
            "status": "SUCCESS", "response": json.dumps(payload())
        }))
        self.assertEqual(result["verdict"], "KEEP")

    def test_accepts_valid_keep(self):
        result = ai_review._validate(payload(), duration=20)
        self.assertEqual(result["verdict"], "KEEP")
        self.assertEqual(result["status"], "reviewed")

    def test_contradictory_keep_score_becomes_review(self):
        self.assertEqual(ai_review._validate(payload("KEEP", 42), 20)["verdict"], "REVIEW")

    def test_contradictory_reject_score_becomes_review(self):
        self.assertEqual(ai_review._validate(payload("REJECT", 82), 20)["verdict"], "REVIEW")

    def test_invalid_verdict_is_rejected(self):
        with self.assertRaises(ValueError):
            ai_review._validate(payload("DELETE", 5), 20)

    def test_invalid_score_is_rejected(self):
        bad = payload()
        bad["score"] = True
        with self.assertRaises(ValueError):
            ai_review._validate(bad, 20)

    def test_out_of_range_moments_are_omitted(self):
        bad = payload()
        bad["moments"] = [
            {"start": -1, "end": 2, "observation": "Outside start"},
            {"start": 3, "end": 99, "observation": "Outside end"},
            {"start": 4, "end": 5, "observation": "Valid"},
        ]
        result = ai_review._validate(bad, 20)
        self.assertEqual(len(result["moments"]), 1)
        self.assertEqual(result["moments"][0]["observation"], "Valid")


if __name__ == "__main__":
    unittest.main()
