from django.db import connections

from django.core.management.base import BaseCommand
import logging
from django.contrib.gis.geos import Point, Polygon

from apps.authentication.models import *
from apps.people.models import *
from apps.segmentation.models import *

import json

logger = logging.getLogger(__name__)
'''
    normal command:
        python manage.py migarte_data_from_old_2_new_table 
        {path to json file}
        --old_table {old_table} --new_table {new_table}
        --model {model_name} --new_unique_field {new_unique_field}
        --old_unique_field {old_unique_field} --lost_table {lost_table}
        
    repair command has --repair argument 
    
    notice: all -- field are optional
'''

error_strings = {}


class Command(BaseCommand):
    base_path = ""
    lost_data = 0
    new_table = "new_table"
    new_unique_field = "new_unique_field"
    old_unique_field = "old_unique_field"
    repair_connection = "production"
    insert_lost_connection = "default"
    lost_table = "lost_segments"
    old_table = "old_table"
    model = ZoneManager
    repair = False
    has_multiple_fields = False
    multiple_fields = []
    map_fields = {}
    static_fields = []
    has_static_fields = False
    after_actions = []
    has_after_actions = False
    optional_arguments = [
        'repair',
        'old_table',
        'lost_table',
        'new_table',
        'model', 
        'old_unique_field',
        'new_unique_field'
    ]

    def get_count_query(self):
        table_name = self.old_table
        if self.repair:
            table_name = self.lost_table
        return "select count(*) from %s" % table_name

    def get_data_query(self, limit, loop_number):
        if self.repair:
            query = "select * from %s where id in (select id from %s limit %s offset %s)" % \
                    (self.old_table, self.lost_table, limit, limit*loop_number)
        else:
            query = "select * from %s limit %s offset %s" % (self.old_table, limit, limit*loop_number)
        return query

    def set_after_actions(self, saved_object, row):
        for after_action in self.after_actions:
            inputs = {}
            for value in after_action["inputs"]:
                inputs[value] = eval(value)
            try:
                eval(after_action["func"])(**inputs)
            except BaseException as e:
                print(str(e), saved_object.old_id)

    def set_static_fields(self, insert_params):
        for static_field in self.static_fields:
            if "func" in static_field:
                inputs = {}
                for value in static_field.inputs:
                    inputs[value] = insert_params[value]
                insert_params[static_field["key"]] = eval(static_field["func"])(**inputs)
            else:
                insert_params[static_field["key"]] = static_field["value"]
        return insert_params

    def add_arguments(self, parser):
        parser.add_argument('path')
        for optional_argument in self.optional_arguments:
            parser.add_argument(
                '--' + optional_argument
            )

    @staticmethod
    def get_connection(connection_name="default"):
        if connection_name in connections:
            return connections[connection_name]
        else:
            return connections['default']

    @staticmethod
    def convert_2_int(string, id, default, minimum, maximum, old_table=''):
        maximum_value = 2 ** 32 if not maximum else maximum
        minimum_value = 0 if not minimum else minimum
        try:
            final_value = int(string) if minimum_value <= int(string) <= maximum_value else default
            return final_value
        except:
            logger.error("cant , convert string='%s' to int in %s table with id=%s" % (string, old_table, id))
            return default

    def convert_unicode(self, string, id):
        try:
            string = string.encode("cp1252", errors="ignore").decode("utf-8", errors='ignore')
        except BaseException as e:
            logger.error('Failed because of: '+ str(e))
            logger.error("cant convert unicode for string=%s in table %s with id=%s" % (string, self.old_table, id))
            pass
        return string

    def calculate_multi_fields(self, row):
        insert_params = {}
        if not self.has_multiple_fields:
            return insert_params
        for multiple_field in self.multiple_fields:
            inputs = {}
            for value in multiple_field["inputs"]:
                inputs[value] = eval(value)
            insert_params[multiple_field["model_field"]] = eval(multiple_field["func"])(**inputs)
        return insert_params

    def store(self, row):
        insert_params = self.calculate_multi_fields(row)
        for key, value in row.items():
            if key not in self.map_fields or not self.map_fields[key]["need"]:
                continue
            if "func" in self.map_fields[key]:
                value = eval(self.map_fields[key]["func"])(value)
            if "map" not in self.map_fields[key]:
                if self.map_fields[key]['convert_to_int']:
                    final_value = self.convert_2_int(
                        value,
                        row["id"], self.map_fields[key]['default'],
                        self.map_fields[key]['min_int'],
                        self.map_fields[key]['max_int'],
                        old_table=self.old_table
                    )
                else:
                    final_value = self.convert_unicode(value, row["id"]) if self.map_fields[key]['unicode_convert'] else value
                insert_params[self.map_fields[key]['model_field']] = final_value
            else:
                try:
                    maps = self.map_fields[key]['map']
                    for k, v in maps.items():
                        if str(k).isdigit():
                            maps[int(k)] = v
                    insert_params[self.map_fields[key]['model_field']] = maps[value]
                except:
                    logger.error("cant find appropriate map_fields key  for value %s and key %s" % (value, key))
        if self.has_static_fields:
            insert_params = self.set_static_fields(insert_params)
        try:
            saved_object = self.model.objects.create(**insert_params)
            if self.has_after_actions:
                self.set_after_actions(saved_object, row)
            return True
        except BaseException as e:
            Command.exception_log(str(e))
            logger.error("cant save dictionary %s for id=%s in %s table" % (str(insert_params), self.old_table, row['id']))
            return False

    @staticmethod
    def convert_result_2_list_of_dict(cursor_obj):
        field_list = [field.name for field in cursor_obj.description]
        result = [dict(zip(field_list, row)) for row in cursor_obj.fetchall()]
        return result

    def init_repair(self):
        production_connection = self.get_connection("production")
        local_connection = self.get_connection("default")
        insert_lost_connection = self.get_connection(self.insert_lost_connection)
        with local_connection.cursor() as local_cursor, production_connection.cursor() as production_cursor, insert_lost_connection.cursor() as insert_lost_cursor:
            insert_lost_cursor.execute("create table if not exists %s(id int)" % self.lost_table)
            insert_lost_cursor.execute("delete from %s" % self.lost_table)
            fetch_fail_id_query = "SELECT id FROM {old_table} where {old_unique_field} in " \
                                  "(SELECT {old_unique_field} FROM {old_table} " \
                                  "EXCEPT SELECT {new_unique_field} FROM {new_table}"
            fetch_fail_id_query = fetch_fail_id_query.format(
                old_table=self.old_table,
                new_table=self.new_table,
                old_unique_field=self.old_unique_field,
                new_unique_field=self.new_unique_field
            )
            production_cursor.execute(fetch_fail_id_query)
            tmp_lost_ids = production_cursor.fetchall()
            lost_ids = [(lost_id[0], ) for lost_id in tmp_lost_ids]
            query = "insert into {} (id) values (%s)"
            query = query.format(self.lost_table)
            insert_lost_cursor.executemany(query, lost_ids)

    def init_config(self, *args, **options):
        if options['path']:
            self.base_path = options['path']
        for optional_argument in self.optional_arguments:
            if optional_argument not in options or not options[optional_argument]:
                continue
            if optional_argument == "model":
                setattr(self, "model", eval(options["model"]))
            elif optional_argument == "repair":
                self.repair = True
            else:
                setattr(self, optional_argument, options[optional_argument])
        with open(self.base_path) as jsonfile:
            map_fields = json.load(jsonfile)
            comment_keys = []
            for key, value in map_fields.items():
                if key.startswith("_"):
                    comment_keys.append(key)
            for comment_key in comment_keys:
                del(map_fields[comment_key])
            self.map_fields = map_fields
        if "static_fields" in self.map_fields:
            self.has_static_fields = True
            self.static_fields = self.map_fields["static_fields"]
            del self.map_fields["static_fields"]
        if "after_actions" in self.map_fields:
            self.has_after_actions = True
            self.after_actions = self.map_fields["after_actions"]
            del self.map_fields["after_actions"]
        if "multiple_fields" in self.map_fields:
            self.has_multiple_fields = True
            self.multiple_fields = self.map_fields["multiple_fields"]
            del self.map_fields["multiple_fields"]
        if self.repair:
            self.init_repair()

    def handle(self, *args, **options):
        self.init_config(*args, **options)
        connection = self.get_connection("production")
        with connection.cursor() as cursor:
            cursor.execute(self.get_count_query())
            count = cursor.fetchone()[0]
            limit = 10000
            loop_number = 0
            cnt = 0
            while loop_number * limit < count:
                logger.info("storing row between (%s, %s)" % (loop_number * limit, (loop_number + 1) * limit))
                cursor.execute(self.get_data_query(limit, loop_number))
                results = Command.convert_result_2_list_of_dict(cursor)
                loop_number += 1
                for row in results:
                    response = self.store(row=row)
                    print(cnt)
                    cnt += 1
                    if not response:
                        self.lost_data += 1
            logger.error("lost data count is %s" % self.lost_data)
            return

    @staticmethod
    def exception_log(error_string):
        has_detail = error_string.find('DETAIL')
        error_string = error_string if not has_detail else error_string[0: has_detail]
        if error_string not in error_strings:
            error_strings[error_string] = 1
        else:
            error_strings[error_string] += 1
        logger.error("cant store because of " + error_string)
