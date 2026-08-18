improvement-2:
-> Tab-2 Transformations: split the single Ports into Input_Ports and Output_Ports
and need one more column named Additional information
so now it becomes: 
 output expected :
column-1: Transformation Name
column-2: Transformation Type
column-3: Business Logic (full logic)
column-4: Additional Informations  
column-5: Input Ports (field involved in that Transformation or Transformation Logic)
column-6: Output Ports (field involved in that Transformation or Transformation Logic)
column-7: Custom / Variable Ports (fields stores logic or like o_* or i_* -this patterns not applicable for Target table fields)

note so the rows in the Transformation Tab should be unique which means:
if user clicks EXP_RESUBMISSION[Expression]
the input_ports and output ports and variable ports involved for that EXCHANGEDPRODUCTTYPE needed.
if user clicks EXP_RESUBMISSION[Expression] of different row then it should points to another row (respective)

even though EXP_RESUBMISSION[Expression] same for above 2 case : but  input_ports and output ports and variable ports will be different
hope u undsertand my requirement




-> need another script that need to extract the details from that excel and place the value here for the below
column-3: Business Logic (full logic)
column-4: Additional Informations  


that excel structure will be Transformations per Tab and Mapplet per tab :
for each transformation dedicated tab will be present and same for mapplet. 

expectation:
for example: EXPTRANS1 from Transformation Tab (created from XML)

- check the exact name in that user provide excel under respective Transformation Tab (here it is expression so find in the expression tab).

column-3: Business Logic (full logic)
for this column  i need this details extracted from the user excel:
if it is Expression then goto Expression Tab ->find header Expression

