-- =============================================================
-- CarePath Schema — Clean Version
-- MySQL | All issues from review resolved
-- =============================================================
CREATE DATABASE IF NOT EXISTS carepath
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE carepath;
SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS `schools`;
DROP TABLE IF EXISTS `parents`;
DROP TABLE IF EXISTS `counselors`;
DROP TABLE IF EXISTS `students`;
DROP TABLE IF EXISTS `roadmap`;
DROP TABLE IF EXISTS `referrals`;
DROP TABLE IF EXISTS `parent_tasks`;
DROP TABLE IF EXISTS `behavior_checks`;
DROP TABLE IF EXISTS `appointment`;
SET FOREIGN_KEY_CHECKS = 1;
-- -------------------------------------------------------------
-- schools
-- -------------------------------------------------------------

CREATE TABLE `schools` (
    `schoolid`  BIGINT       NOT NULL AUTO_INCREMENT,
    `name`      VARCHAR(255) NOT NULL,
    `location`  VARCHAR(255) NOT NULL,
    `district`  VARCHAR(255) NOT NULL,
    PRIMARY KEY (`schoolid`)
);

-- -------------------------------------------------------------
-- parents
-- -------------------------------------------------------------
CREATE TABLE `parents` (
    `parentid`  BIGINT NOT NULL AUTO_INCREMENT,
    `fname`     TEXT   NOT NULL,
    `lname`     TEXT   NOT NULL,
    `email`     TEXT   NOT NULL,
    `password`  TEXT   NOT NULL,
    PRIMARY KEY (`parentid`)
);

-- -------------------------------------------------------------
-- counselors
-- -------------------------------------------------------------
CREATE TABLE `counselors` (
    `counselorid`  BIGINT NOT NULL AUTO_INCREMENT,
    `fname`        TEXT   NOT NULL,
    `lname`        TEXT   NOT NULL,
    `email`        TEXT   NOT NULL,
    `schoolid`     BIGINT NOT NULL,
    PRIMARY KEY (`counselorid`)
);

-- -------------------------------------------------------------
-- students
-- -------------------------------------------------------------
CREATE TABLE `students` (
    `studentid`  BIGINT NOT NULL AUTO_INCREMENT,
    `fname`      TEXT   NOT NULL,
    `lname`      TEXT   NOT NULL,
    `schoolid`   BIGINT NOT NULL,
    `parentid`   BIGINT NOT NULL,
    PRIMARY KEY (`studentid`)
);

-- -------------------------------------------------------------
-- roadmap
-- 1-to-1 with students enforced via UNIQUE on studentid
-- -------------------------------------------------------------
CREATE TABLE `roadmap` (
    `roadmapid`   BIGINT NOT NULL AUTO_INCREMENT,
    `studentid`   BIGINT NOT NULL,
    `counselorid` BIGINT NOT NULL,
    `status`      ENUM('active', 'paused', 'completed') NOT NULL DEFAULT 'active',
    `created_at`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`roadmapid`),
    UNIQUE KEY `roadmap_studentid_unique` (`studentid`)   -- enforces 1-to-1
);

-- -------------------------------------------------------------
-- referrals
-- Tracks facilities the counselor refers the student to.
-- Status tracks whether the parent followed through.
-- -------------------------------------------------------------
CREATE TABLE `referrals` (
    `referralid`  BIGINT NOT NULL AUTO_INCREMENT,
    `roadmapid`   BIGINT NOT NULL,
    `therapy_type` VARCHAR(255) NOT NULL,
    `name`        TEXT   NOT NULL,
    `location`    TEXT   NOT NULL,
    `status`      ENUM('pending', 'scheduled', 'checked_in', 'completed', 'missed')
                  NOT NULL DEFAULT 'pending',
    PRIMARY KEY (`referralid`)
);

-- -------------------------------------------------------------
-- parent_tasks
-- Steps the parent needs to complete as part of the roadmap.
-- Duplicate roadmapid column removed, status converted to ENUM.
-- -------------------------------------------------------------
CREATE TABLE `parent_tasks` (
    `parent_taskid`  BIGINT NOT NULL AUTO_INCREMENT,
    `roadmapid`      BIGINT NOT NULL,
    `description`    TEXT   NOT NULL,
    `stage`          ENUM('stage_1', 'stage_2', 'stage_3', 'stage_4', 'stage_5', 'stage_6') NOT NULL DEFAULT 'stage_1',
    `status`         ENUM('pending', 'in_progress', 'completed')
                     NOT NULL DEFAULT 'pending',
    PRIMARY KEY (`parent_taskid`)
);

-- -------------------------------------------------------------
-- behavior_checks
-- Behavior markers tied to a parent task.
-- roadmapid removed (reachable via parent_tasks join).
-- FK direction fixed: behavior_checks references parent_tasks.
-- -------------------------------------------------------------
CREATE TABLE `behavior_checks` (
    `behavior_checkid`  BIGINT NOT NULL AUTO_INCREMENT,
    `parent_taskid`     BIGINT NOT NULL,
    `description`       TEXT   NOT NULL,
    `stage`             int NOT NULL,
    `status`            ENUM('not_met', 'in_progress', 'met')
                        NOT NULL DEFAULT 'not_met',
    PRIMARY KEY (`behavior_checkid`)
);

-- -------------------------------------------------------------
-- appointment
-- Tracks scheduled meetings between parents and counselors.
-- -------------------------------------------------------------
CREATE TABLE `appointment` (
    `appointmentid`  BIGINT   NOT NULL AUTO_INCREMENT,
    `parentid`       BIGINT   NOT NULL,
    `counselorid`    BIGINT   NOT NULL,
    `createdBy`      ENUM('parent', 'counselor') NOT NULL DEFAULT 'parent',
    `status`         ENUM('pending', 'confirmed', 'cancelled') NOT NULL DEFAULT 'pending',
    `start`          DATETIME NOT NULL,
    `end`            DATETIME NOT NULL,
    PRIMARY KEY (`appointmentid`)
);

-- =============================================================
-- FOREIGN KEYS
-- =============================================================

-- students → schools
ALTER TABLE `students`
    ADD CONSTRAINT `students_schoolid_foreign`
    FOREIGN KEY (`schoolid`) REFERENCES `schools` (`schoolid`);

-- students → parents
ALTER TABLE `students`
    ADD CONSTRAINT `students_parentid_foreign`
    FOREIGN KEY (`parentid`) REFERENCES `parents` (`parentid`);

-- counselors → schools
ALTER TABLE `counselors`
    ADD CONSTRAINT `counselors_schoolid_foreign`
    FOREIGN KEY (`schoolid`) REFERENCES `schools` (`schoolid`);

-- roadmap → students (1-to-1 enforced by UNIQUE above)
ALTER TABLE `roadmap`
    ADD CONSTRAINT `roadmap_studentid_foreign`
    FOREIGN KEY (`studentid`) REFERENCES `students` (`studentid`);

-- roadmap → counselors
ALTER TABLE `roadmap`
    ADD CONSTRAINT `roadmap_counselorid_foreign`
    FOREIGN KEY (`counselorid`) REFERENCES `counselors` (`counselorid`);

-- referrals → roadmap
ALTER TABLE `referrals`
    ADD CONSTRAINT `referrals_roadmapid_foreign`
    FOREIGN KEY (`roadmapid`) REFERENCES `roadmap` (`roadmapid`);

-- parent_tasks → roadmap
ALTER TABLE `parent_tasks`
    ADD CONSTRAINT `parent_tasks_roadmapid_foreign`
    FOREIGN KEY (`roadmapid`) REFERENCES `roadmap` (`roadmapid`);

-- behavior_checks → parent_tasks (direction fixed)
ALTER TABLE `behavior_checks`
    ADD CONSTRAINT `behavior_checks_parent_taskid_foreign`
    FOREIGN KEY (`parent_taskid`) REFERENCES `parent_tasks` (`parent_taskid`);

-- appointment → parents
ALTER TABLE `appointment`
    ADD CONSTRAINT `appointment_parentid_foreign`
    FOREIGN KEY (`parentid`) REFERENCES `parents` (`parentid`);

-- appointment → counselors
ALTER TABLE `appointment`
    ADD CONSTRAINT `appointment_counselorid_foreign`
    FOREIGN KEY (`counselorid`) REFERENCES `counselors` (`counselorid`);
-- =============================================================
-- CarePath Dummy Data
-- Insert order respects FK dependencies
-- =============================================================

-- -------------------------------------------------------------
-- schools
-- -------------------------------------------------------------
INSERT INTO `schools` (`schoolid`, `name`, `location`, `district`) VALUES
(1, 'Riverside High School',      '1200 Riverside Dr, Atlanta, GA 30310',    'Atlanta Public Schools'),
(2, 'Northside Elementary',       '450 Northside Pkwy, Atlanta, GA 30318',   'Atlanta Public Schools'),
(3, 'Peachtree Middle School',    '800 Peachtree St, Atlanta, GA 30308',     'Fulton County Schools'),
(4, 'Gwinnett Academy',           '2200 Campus Dr, Lawrenceville, GA 30043', 'Gwinnett County Schools'),
(5, 'Westlake High School',       '2400 Union Rd, Atlanta, GA 30331',        'Atlanta Public Schools');

-- -------------------------------------------------------------
-- parents
-- -------------------------------------------------------------
INSERT INTO `parents` (`parentid`, `fname`, `lname`, `email`, `password`) VALUES
(1, 'Linda',    'Thompson',  'linda.thompson@gmail.com',   'hashed_password_1'),
(2, 'Marcus',   'Williams',  'marcus.williams@gmail.com',  'hashed_password_2'),
(3, 'Denise',   'Carter',    'denise.carter@yahoo.com',    'hashed_password_3'),
(4, 'James',    'Robinson',  'james.robinson@gmail.com',   'hashed_password_4'),
(5, 'Patricia', 'Johnson',   'patricia.j@hotmail.com',     'hashed_password_5');

-- -------------------------------------------------------------
-- counselors
-- -------------------------------------------------------------
INSERT INTO `counselors` (`counselorid`, `fname`, `lname`, `email`, `schoolid`) VALUES
(1, 'Patricia', 'Cole',      'p.cole@atlantaschools.edu',      1),
(2, 'David',    'Hernandez', 'd.hernandez@atlantaschools.edu', 2),
(3, 'Angela',   'Rivera',    'a.rivera@fultonschools.edu',     3),
(4, 'Marcus',   'Grant',     'm.grant@gwinnettschools.edu',    4),
(5, 'Susan',    'Park',      's.park@atlantaschools.edu',      5);

-- -------------------------------------------------------------
-- students
-- -------------------------------------------------------------
INSERT INTO `students` (`studentid`, `fname`, `lname`, `schoolid`, `parentid`) VALUES
(1, 'Marcus',   'Thompson',  1, 1),
(2, 'Aisha',    'Williams',  2, 2),
(3, 'DeShawn',  'Carter',    3, 3),
(4, 'Brianna',  'Robinson',  4, 4),
(5, 'Elijah',   'Johnson',   5, 5);

-- -------------------------------------------------------------
-- roadmap
-- One roadmap per student (1-to-1 enforced by UNIQUE)
-- -------------------------------------------------------------
INSERT INTO `roadmap` (`roadmapid`, `studentid`, `counselorid`, `status`, `created_at`) VALUES
(1, 1, 1, 'active',    '2025-09-04 09:00:00'),
(2, 2, 2, 'active',    '2025-09-10 10:30:00'),
(3, 3, 3, 'paused',    '2025-08-15 08:00:00'),
(4, 4, 4, 'active',    '2025-09-18 11:00:00'),
(5, 5, 5, 'completed', '2025-07-01 09:00:00');

-- -------------------------------------------------------------
-- referrals
-- Facilities the counselor has referred the student to
-- -------------------------------------------------------------
INSERT INTO `referrals` (`referralid`, `roadmapid`, `therapy_type`, `name`, `location`, `status`) VALUES
(1, 1, 'behavioral_health', 'Grady Behavioral Health',          '80 Jesse Hill Jr Dr, Atlanta, GA 30303',     'checked_in'),
(2, 1, 'speech_language_therapy', 'Atlanta Speech & Language Center', '1100 Lake Hearn Dr, Atlanta, GA 30342',      'pending'),
(3, 2, 'pediatrics', 'Northside Hospital Pediatrics',    '1000 Johnson Ferry Rd, Atlanta, GA 30342',   'scheduled'),
(4, 3, 'mental_health', 'DeKalb Community Mental Health',   '445 Winn Way, Decatur, GA 30030',            'missed'),
(5, 4, 'pediatrics', 'Gwinnett Pediatric Therapy',       '600 Professional Dr, Lawrenceville, GA 30046','pending'),
(6, 5, 'family_counseling', 'Westside Family Counseling',       '1500 Donald Lee Hollowell Pkwy, GA 30318',   'completed');

-- -------------------------------------------------------------
-- parent_tasks
-- Action steps assigned to parents as part of the roadmap
-- -------------------------------------------------------------
INSERT INTO `parent_tasks` (`parent_taskid`, `roadmapid`, `description`, `stage`, `status`) VALUES
(1,  1, 'Establish a consistent morning routine with a visual schedule posted in the kitchen.',                           'stage_1', 'completed'),
(2,  1, 'Read aloud with Marcus for 20 minutes every evening using his chosen book.',                                     'stage_2', 'in_progress'),
(3,  1, 'Attend the first parent-counselor check-in meeting at Riverside High School.',                                   'stage_3', 'pending'),
(4,  2, 'Reduce screen time to 1 hour per day and replace with outdoor activity.',                                        'stage_1', 'completed'),
(5,  2, 'Use a reward chart to reinforce Aisha completing homework before dinner.',                                       'stage_2', 'in_progress'),
(6,  2, 'Schedule and attend first therapy session at Northside Hospital Pediatrics.',                                    'stage_3', 'pending'),
(7,  3, 'Attend monthly IEP review meeting at Peachtree Middle School.',                                                  'stage_1', 'pending'),
(8,  3, 'Practice calming strategies with DeShawn using the provided sensory toolkit.',                                   'stage_2', 'in_progress'),
(9,  4, 'Create a homework station at home free of distractions and consistent every day.',                               'stage_1', 'completed'),
(10, 4, 'Review Brianna''s planner nightly and sign off on completed assignments.',                                       'stage_2', 'in_progress'),
(11, 5, 'Complete all 6 sessions at Westside Family Counseling.',                                                         'stage_1', 'completed'),
(12, 5, 'Maintain the bedtime routine established during Stage 1 into the maintenance phase.',                            'stage_2', 'completed'),
(13, 3, 'Schedule and attend the recommended community mental health intake appointment.',                                'stage_3', 'pending'),
(14, 4, 'Attend the next school counselor check-in and bring Brianna''s assignment progress updates.',                    'stage_3', 'pending'),
(15, 5, 'Schedule a maintenance follow-up session to support continued progress.',                                        'stage_3', 'pending');

-- -------------------------------------------------------------
-- behavior_checks 
-- Observable markers tied to each parent task
-- -------------------------------------------------------------
INSERT INTO `behavior_checks` (`behavior_checkid`, `parent_taskid`, `description`, `stage`, `status`) VALUES
(1,  1,  'Marcus wakes up on time without repeated reminders 4 out of 5 school days.',                       1, 'met'),
(3,  2,  'Marcus reads for 20 minutes without prompting at least 4 nights per week.',                        2, 'in_progress'),
(5,  4,  'Aisha spends no more than 1 hour on screens on school nights for 2 consecutive weeks.',             1, 'met'),
(6,  5,  'Aisha completes homework before dinner 4 out of 5 nights per week.',                               2, 'in_progress'),
(8,  8,  'DeShawn uses a calming strategy independently when frustrated at least once per week.',             2, 'in_progress'),
(10, 9,  'Brianna uses the homework station every school day for 2 consecutive weeks.',                       1, 'met'),
(11, 10, 'Brianna''s planner is signed by parent 4 out of 5 nights per week.',                               2, 'in_progress'),
(12, 11, 'Elijah attends all 6 scheduled counseling sessions with no cancellations.',                         1, 'met'),
(13, 12, 'Elijah maintains consistent bedtime (9pm or earlier) for 30 consecutive days.',                    2, 'met'),
(14, 3,  'Marcus attends the check-in meeting as scheduled (no missed appointment).',                         3, 'not_met'),
(15, 6,  'Therapy session is scheduled and attended within the next 2 weeks.',                                3, 'not_met'),
(16, 7,  'IEP review meeting is attended and next steps are documented.',                                     1, 'not_met'),
(17, 13, 'Intake appointment is scheduled and completed (date confirmed).',                                  3, 'not_met'),
(18, 14, 'Counselor check-in is completed and planner updates are reviewed that day.',                        3, 'not_met'),
(19, 15, 'Follow-up session is scheduled and completed within 30 days.',                                     3, 'not_met');

-- -------------------------------------------------------------
-- appointment
-- Scheduled meetings between parents and counselors
-- -------------------------------------------------------------
INSERT INTO `appointment` (`appointmentid`, `parentid`, `counselorid`, `createdBy`, `status`, `start`, `end`) VALUES
(1, 1, 1, 'parent', 'confirmed', '2025-09-15 10:00:00', '2025-09-15 10:30:00'),
(2, 1, 1, 'parent', 'confirmed', '2025-10-13 10:00:00', '2025-10-13 10:30:00'),
(3, 2, 2, 'parent', 'confirmed', '2025-09-20 14:00:00', '2025-09-20 14:30:00'),
(4, 3, 3, 'parent', 'confirmed', '2025-09-05 09:00:00', '2025-09-05 09:30:00'),
(5, 4, 4, 'parent', 'confirmed', '2025-09-25 11:00:00', '2025-09-25 11:30:00'),
(6, 5, 5, 'parent', 'confirmed', '2025-08-01 13:00:00', '2025-08-01 13:30:00'),
(7, 2, 2, 'parent', 'confirmed', '2025-10-18 14:00:00', '2025-10-18 14:30:00'),
(8, 1, 1, 'parent', 'confirmed', '2025-11-10 10:00:00', '2025-11-10 10:30:00'),
(9, 1, 1, 'counselor', 'confirmed', '2025-09-15 10:00:00', '2025-09-15 10:30:00'),
(10, 1, 1, 'counselor', 'confirmed', '2025-10-13 10:00:00', '2025-10-13 10:30:00'),
(11, 2, 2, 'counselor', 'confirmed', '2025-09-20 14:00:00', '2025-09-20 14:30:00'),
(12, 3, 3, 'counselor', 'confirmed', '2025-09-05 09:00:00', '2025-09-05 09:30:00'),
(13, 4, 4, 'counselor', 'confirmed', '2025-09-25 11:00:00', '2025-09-25 11:30:00'),
(14, 5, 5, 'counselor', 'confirmed', '2025-08-01 13:00:00', '2025-08-01 13:30:00'),
(15, 2, 2, 'counselor', 'confirmed', '2025-10-18 14:00:00', '2025-10-18 14:30:00'),
(16, 1, 1, 'counselor', 'confirmed', '2025-11-10 10:00:00', '2025-11-10 10:30:00');