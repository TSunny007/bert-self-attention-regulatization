export OG_DIR='../conll003-englishversion'
export MAX_LENGTH=129
export BERT_MODEL=bert-base-multilingual-cased
python3 preprocess.py $OG_DIR/train.txt $BERT_MODEL $MAX_LENGTH > train.txt
python3 preprocess.py $OG_DIR/valid.txt $BERT_MODEL $MAX_LENGTH > dev.txt
python3 preprocess.py $OG_DIR/test.txt $BERT_MODEL $MAX_LENGTH > test.txt
cat train.txt dev.txt test.txt | cut -d " " -f 2 | grep -v "^$"| sort | uniq > labels.txt
