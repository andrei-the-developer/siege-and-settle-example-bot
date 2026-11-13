"""Tests of the bot's decisions. They need no game server: python3 -m unittest -v"""

import unittest

from siege_bot import (
    BARRACKS,
    GRANARY,
    MAIN_BUILDING,
    WAREHOUSE,
    Rules,
    choose_build,
    choose_training,
    planned_levels,
    reward_fits,
    tutorial_plan,
)


def level(n, cost):
    return {"level": n, "cost": cost}


# A tiny rule set: four fields (one per resource), the storage, the main building and the barracks.
RULES = Rules({
    "limits": {"field_slots": 4},
    "buildings": [
        {"gid": g, "max_level": 10, "requirements": {"multiple": True},
         "levels": [level(n, [10 * n, 10 * n, 10 * n, 10 * n]) for n in range(11)]}
        for g in (1, 2, 3, 4)
    ] + [
        {"gid": WAREHOUSE, "max_level": 20, "requirements": {"levels": [[MAIN_BUILDING, 1]]},
         "levels": [level(n, [50, 50, 50, 50]) for n in range(21)]},
        {"gid": GRANARY, "max_level": 20, "requirements": {"levels": [[MAIN_BUILDING, 1]]},
         "levels": [level(n, [40, 40, 40, 40]) for n in range(21)]},
        {"gid": MAIN_BUILDING, "max_level": 20, "requirements": {},
         "levels": [level(n, [30, 30, 30, 30]) for n in range(21)]},
        {"gid": BARRACKS, "max_level": 20, "requirements": {"levels": [[MAIN_BUILDING, 3]]},
         "levels": [level(n, [200, 200, 200, 200]) for n in range(21)]},
    ],
})


def village(**kw):
    v = {
        "id": 1,
        "resources": [500, 500, 500, 500],
        "production": [10, 10, 10, 10],
        "max_store": 800,
        "max_crop": 800,
        "queue": {"max": 5},
        "jobs": [],
        "slots": [
            {"slot": 1, "gid": 1, "level": 2},
            {"slot": 2, "gid": 2, "level": 1},
            {"slot": 3, "gid": 3, "level": 2},
            {"slot": 4, "gid": 4, "level": 2},
            {"slot": 19, "gid": 0, "level": 0},
            {"slot": 20, "gid": 0, "level": 0},
            {"slot": 26, "gid": MAIN_BUILDING, "level": 3},
        ],
    }
    v.update(kw)
    return v


class ChooseBuild(unittest.TestCase):
    def test_lowest_field_first(self):
        b = choose_build(village(), RULES)
        self.assertEqual((b.action, b.slot, b.level), ("upgrade", 2, 2))

    def test_queued_job_counts_as_built(self):
        # slot 2 is already queued to level 2: the next lowest is another field, never slot 2 to level 3 at once
        v = village(jobs=[{"Slot": 2, "Gid": 2, "Level": 2}])
        self.assertEqual(planned_levels(v)[2], (2, 2))
        b = choose_build(v, RULES)
        self.assertNotEqual(b.slot, 2)

    def test_full_queue_waits(self):
        v = village(jobs=[{"Slot": 1, "Gid": 1, "Level": 3}, {"Slot": 3, "Gid": 3, "Level": 3}])
        self.assertIsNone(choose_build(v, RULES, max_queue=2))

    def test_one_more_job_when_stores_overflow(self):
        jobs = [{"Slot": 1, "Gid": 1, "Level": 3}, {"Slot": 3, "Gid": 3, "Level": 3}]
        self.assertIsNotNone(choose_build(village(jobs=jobs, resources=[760, 500, 500, 500]), RULES, max_queue=2))

    def test_storage_when_nearly_full(self):
        b = choose_build(village(resources=[700, 100, 100, 100]), RULES)
        self.assertEqual((b.action, b.gid, b.slot), ("construct", WAREHOUSE, 19))

    def test_crop_first_when_food_is_short(self):
        b = choose_build(village(production=[10, 10, 10, 2]), RULES)
        self.assertEqual((b.slot, b.gid), (4, 4))

    def test_building_that_would_starve_waits_for_crop(self):
        rules = Rules({"limits": {"field_slots": 4}, "buildings": [
            dict(b, levels=[dict(lv, pop=4) for lv in b["levels"]]) for b in RULES.buildings.values()]})
        goals = [{"kind": "building", "gid": MAIN_BUILDING, "level": 4}]
        b = choose_build(village(production=[10, 10, 10, 3]), rules, goals)
        self.assertEqual((b.gid, b.reason), (4, "crop surplus is low"))

    def test_tutorial_goal_first_and_not_twice(self):
        goals = [{"kind": "building", "gid": MAIN_BUILDING, "level": 4}]
        b = choose_build(village(), RULES, goals)
        self.assertEqual((b.slot, b.level, b.reason), (26, 4, "tutorial goal"))
        queued = village(jobs=[{"Slot": 26, "Gid": MAIN_BUILDING, "Level": 4}])
        self.assertNotEqual(choose_build(queued, RULES, goals).reason, "tutorial goal")

    def test_requirements_block_construction(self):
        v = village(slots=[s for s in village()["slots"] if s["gid"] != MAIN_BUILDING])
        b = choose_build(v, RULES, [{"kind": "building", "gid": BARRACKS, "level": 1}])
        self.assertNotEqual(b.gid, BARRACKS)

    def test_nothing_affordable(self):
        self.assertIsNone(choose_build(village(resources=[5, 5, 5, 5]), RULES))


class Tutorial(unittest.TestCase):
    STEPS = {
        "enabled": True,
        "steps": [
            {"id": 1, "title": "a", "met": True, "claimed": True, "condition": {"kind": "building", "gid": 1, "level": 1}},
            {"id": 2, "title": "b", "met": True, "claimed": False, "reward": {"wood": 100, "clay": 100, "iron": 100, "crop": 100},
             "condition": {"kind": "resource_fields"}},
            {"id": 3, "title": "c", "met": False, "claimed": False, "condition": {"kind": "building", "gid": 15, "level": 2}},
            {"id": 4, "title": "d", "met": False, "claimed": False, "condition": {"kind": "screen"}},
        ],
    }

    def test_claims_and_goals(self):
        claims, goals = tutorial_plan(self.STEPS, village())
        self.assertEqual(claims, [(2, "b")])
        self.assertEqual(goals, [{"kind": "building", "gid": 15, "level": 2}])

    def test_reward_that_would_be_lost_waits(self):
        claims, _ = tutorial_plan(self.STEPS, village(resources=[750, 100, 100, 100]))
        self.assertEqual(claims, [])

    def test_reward_fits_when_stores_are_nearly_empty(self):
        self.assertTrue(reward_fits({"wood": 5000}, village(resources=[10, 10, 10, 10])))

    def test_finished_tutorial(self):
        self.assertEqual(tutorial_plan({"enabled": True, "finished": True}), ([], []))


class Training(unittest.TestCase):
    # the shape of GET .../train-options: {"building": 19, "units": [...]}
    OPTIONS = [
        {"unit": 21, "cost": [100, 130, 55, 30], "researched": True, "max": 99, "seconds": 1000},
        {"unit": 22, "cost": [140, 150, 185, 60], "researched": True, "max": 99, "seconds": 1300},
        {"unit": 23, "cost": [10, 10, 10, 10], "researched": False, "max": 0, "seconds": 900},
    ]

    def test_cheapest_unit_from_spare_resources(self):
        # half of 800 is kept; the spare 300 clay pays for two of the cheapest unit
        self.assertEqual(choose_training(village(resources=[700, 700, 700, 700]), self.OPTIONS), (21, 2))

    def test_capped_by_max(self):
        options = [dict(self.OPTIONS[0], max=1)]
        self.assertEqual(choose_training(village(resources=[800, 800, 800, 800]), options), (21, 1))

    def test_nothing_spare(self):
        self.assertIsNone(choose_training(village(resources=[400, 400, 400, 400]), self.OPTIONS))


if __name__ == "__main__":
    unittest.main()
