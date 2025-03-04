import os
import json
import torch
import src.utils.file as file_utils
import src.utils.list as list_utils

from typing import List, Tuple, Any
from src.parser.parser import SQLParser
from src.data.schema import SchemaGenerator, Schema, Table, Column
from src.data.data import TextToSQLDataset, TextToSQLDatum
from src.db_connection.sqlite_connector import SqliteConnector

os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Spider Schema Generator
class SpiderSchemaGenerator(SchemaGenerator):
    @staticmethod
    def meta_data_to_schema(schema_datum: Any, database_dir_path: str, tokenizer: Any) -> Schema:
        def add_foreign_key_relations(schema: Schema, foreign_key_pairs: List[Tuple[str, str]]):
            for src_col_ref, dst_col_ref in foreign_key_pairs:
                src_table_name, src_col_name = src_col_ref.split(".")
                dst_table_name, dst_col_name = dst_col_ref.split(".")
                src_column = schema.get_column(src_table_name, src_col_name)
                dst_column = schema.get_column(dst_table_name, dst_col_name)
                src_column.foreign_key_forward.append(dst_column)
                dst_column.foreign_key_backward.append(src_column)
            return schema

        def get_col_ref_from_global_col_id(global_col_id, table_names, column_names_with_tab_id):
            tab_id, col_name = column_names_with_tab_id[global_col_id]
            tab_name = table_names[tab_id]
            return f"{tab_name}.{col_name}"

        # Organize data
        db_id = schema_datum["db_id"]
        ## Exclude "*"
        ori_column_names_with_tab_id = schema_datum["column_names_original"][1:]
        column_names_with_tab_id = schema_datum["column_names"][1:]
        column_types = list(map(Column.data_type_str_to_enum, schema_datum["column_types"][1:]))
        table_names = schema_datum["table_names"]
        ori_table_names = schema_datum["table_names_original"]
        ## Subtract one to re-assign correct index without "*"
        foreign_key_pair_global_ids = list(map(lambda k: [k[0]-1, k[1]-1], schema_datum["foreign_keys"]))
        primary_key_global_ids = list(map(lambda k: k-1, schema_datum["primary_keys"]))

        # Create DB Connector
        db_connector = SqliteConnector(os.path.join(database_dir_path, f"{db_id}/{db_id}.sqlite"))
        # Create Schema object
        schema = Schema(db_id=db_id, db_connector=db_connector, tokenizer=tokenizer)

        # Create Table objects
        for tab_idx, (ori_table_name, table_name) in enumerate(zip(ori_table_names, table_names)):
            table_tokenized_result = tokenizer(table_name, add_special_tokens=False)
            table_name_tokens = tokenizer.convert_ids_to_tokens(table_tokenized_result["input_ids"])
            table_name_tensor = torch.tensor(table_tokenized_result["input_ids"])
            table = Table(db_id=db_id, ori_name=ori_table_name, name=table_name, name_tokens=table_name_tokens, name_tensor=table_name_tensor)
            
            column_names = list(map(lambda k: k[1], filter(lambda k: k[0] == tab_idx, column_names_with_tab_id)))
            ori_column_names = list(map(lambda k: k[1], filter(lambda k: k[0] == tab_idx, ori_column_names_with_tab_id)))
            
            # Get column information
            for ori_col_name, col_name, col_type in zip(ori_column_names, column_names, column_types):
                col_tokenized_result = tokenizer(col_name, add_special_tokens=False)
                col_name_tokens = tokenizer.convert_ids_to_tokens(col_tokenized_result["input_ids"])
                col_name_tensor = torch.tensor(col_tokenized_result["input_ids"])
                col_global_idx = column_names_with_tab_id.index([tab_idx, col_name])
                column = Column(ori_name=ori_col_name, name=col_name, name_tokens=col_name_tokens, name_tensor=col_name_tensor, data_type=col_type, table=table,
                                is_primary_key=col_global_idx in primary_key_global_ids, 
                                foreign_key_forward=[], 
                                foreign_key_backward=[])
                # Save column in the table object
                table.add_column(column)
                
            # Save table in the schema object
            schema.add_table(table)
        
        # Get foreign key pairs by names
        foreign_key_pairs = []
        for src_id, dst_id in foreign_key_pair_global_ids:
            src_col_ref = get_col_ref_from_global_col_id(src_id, table_names, column_names_with_tab_id)
            dst_col_ref = get_col_ref_from_global_col_id(dst_id, table_names, column_names_with_tab_id)
            foreign_key_pairs.append([src_col_ref, dst_col_ref])
        
        # Add foreign key relations
        schema = add_foreign_key_relations(schema, foreign_key_pairs)
        return schema

    @staticmethod 
    def load_schema_from_meta_data_file(meta_data_file_path: str, database_dir_path: str, tokenizer: Any, data_filter_func=None) -> List[Schema]:
        schemas = []
        with open(meta_data_file_path, "r") as meta_data_file:
            meta_data_of_dbs = json.load(meta_data_file)
            filtered_meta_data_of_dbs = list(filter(data_filter_func, meta_data_of_dbs)) if data_filter_func else meta_data_of_dbs
            for filtered_meta_data_of_db in filtered_meta_data_of_dbs:
                schema = SpiderSchemaGenerator.meta_data_to_schema(filtered_meta_data_of_db, database_dir_path, tokenizer)
                schemas.append(schema)
        return schemas

#  Spider Dataset
class SpiderDataset(TextToSQLDataset):
    def __init__(self, file_paths, tokenizer, sql_parser, data_filter_func=None):
        super(SpiderDataset, self).__init__(file_paths=file_paths, tokenizer=tokenizer, sql_parser=sql_parser,
                                            data_filter_func=data_filter_func)

    def _read_in_data_from_files(self, file_paths):
        data_list = map(file_utils.read_json_file, file_paths)
        data = list_utils.flatten_list(data_list)
        return data

    def _raw_datum_to_textToSQLDatum(self, raw_datum):
        schema = Schema.get_cached_object(raw_datum["db_id"])
        sql = raw_datum["query"]
        nl = raw_datum["question"]
        tokenized_result = self.tokenizer(nl, add_special_tokens=False)
        question_tokens = self.tokenizer.convert_ids_to_tokens(tokenized_result["input_ids"])
        question_tensor = torch.tensor(tokenized_result["input_ids"])
        parse_tree=self.sql_parser.sql2ast(sql)
        textToSQLDatum = TextToSQLDatum(schema=schema, sql=sql, parse_tree=parse_tree, nl=nl,
                                        nl_tokens=question_tokens,
                                        nl_tensor=question_tensor,
                                        tokenizer=self.tokenizer)
        return textToSQLDatum


if __name__ == '__main__':
    from transformers import AutoTokenizer
    from torch.utils.data import DataLoader
    from src.data.data import collate_fn

    dir_path="./datasets/spider"
    file_paths = file_utils.get_files_in_directory(dir_path, lambda file_name: file_name.startswith('train') and file_name.endswith('.json'))
    tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")
    sql_parser = SQLParser("./src/grammar/ratsql_extended.asdl")
    SpiderSchemaGenerator.load_schema_from_meta_data_file(os.path.join(dir_path, "tables.json"), os.path.join(dir_path, "database"), tokenizer)
    my_dataset = SpiderDataset(file_paths, tokenizer, sql_parser)
    
    dataloader = DataLoader(dataset=my_dataset, batch_size=2, num_workers=0, collate_fn=collate_fn)
    for data in dataloader:
        print(data)
        break
    print(f"Total len: {len(my_dataset)}")
    