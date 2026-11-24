import unittest
from battery_cycles import process


class BatteryTests(unittest.TestCase):
    def base(self):
        return {"active_current_a":"1","min_phase_seconds":"5","samples":[{"id":"s0","time_s":"0","voltage_v":"10","current_a":"2"},{"id":"s1","time_s":"10","voltage_v":"10","current_a":"2"},{"id":"s2","time_s":"20","voltage_v":"10","current_a":"0"},{"id":"s3","time_s":"30","voltage_v":"10","current_a":"-1"},{"id":"s4","time_s":"40","voltage_v":"10","current_a":"-1"}]}

    def test_segments_by_left_sample_state(self):
        result=process(self.base())
        self.assertEqual([p["state"] for p in result["phases"]],["charge","rest","discharge"])

    def test_integrates_trapezoids(self):
        result=process(self.base())
        self.assertEqual(result["charge_wh"],"1/12")
        self.assertEqual(result["discharge_wh"],"1/36")
        self.assertEqual(result["round_trip_efficiency"],"1/3")

    def test_short_active_phase_is_flagged(self):
        data=self.base(); data["min_phase_seconds"]="15"
        self.assertEqual(process(data)["phases"][2]["status"],"too_short")

    def test_no_charge_has_null_efficiency(self):
        data=self.base()
        for row in data["samples"]: row["current_a"]="-2"
        self.assertIsNone(process(data)["round_trip_efficiency"])

    def test_times_must_increase(self):
        data=self.base(); data["samples"][2]["time_s"]="10"
        with self.assertRaises(ValueError): process(data)

    def test_voltage_must_be_positive(self):
        data=self.base(); data["samples"][0]["voltage_v"]="0"
        with self.assertRaises(ValueError): process(data)


if __name__=="__main__": unittest.main()

