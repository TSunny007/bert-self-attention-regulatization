python3 run_ner.py \
--model_name_or_path bert-base-cased \
--cache_dir /home/sunkaran/scr/sunkaraneni/.cache --data_dir ./data/ --labels ./data/labels.txt \
--max_seq_length  128 \
--output_dir ./results \
--do_train --do_eval --do_predict \
--warmup_steps 263 \
--epochs 5 \
